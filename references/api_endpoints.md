# API Endpoints

Taken from the calculator's own `config.js` (calculator.aws). All are public
CloudFront endpoints; the scripts call them through `curl`.

| Purpose | Method + URL |
|---|---|
| Service catalog | `GET https://d1qsjq9pzbk1k6.cloudfront.net/manifest/en_US.json` |
| Service definition | `GET https://d1qsjq9pzbk1k6.cloudfront.net/data/{serviceCode}/en_US.json` |
| Regions per service | `GET https://d1qsjq9pzbk1k6.cloudfront.net/data/{serviceCode}/regionList/publish.json` (`inclusionList`) |
| Save estimate | `POST https://dnd5zrqcec4or.cloudfront.net/Prod/v2/saveAs` |
| Load saved estimate | `GET https://d3knqfixx3sbls.cloudfront.net/{savedKey}` |
| View in browser | `https://calculator.aws/#/estimate?id={savedKey}` |
| Table pricing combinations | `GET https://calculator.aws/pricing/2.0/meteredUnitMaps/{dir}/USD/current/{mapping}/{Location}/primary-selector-aggregations.json` (gzip) |

Unknown service codes return **403** (S3 AccessDenied XML), not 404.

## Manifest

`{"awsServices": [{"serviceCode", "name", "isActive": "true"|"false",
"disableConfigure", "templates", "searchKeywords", ...}]}`. Only services with
`isActive == "true"` load in the calculator. The manifest's `regions` field is
not a complete region list; use the regionList endpoint.

## Service definition

- `version`: current definition version (the builder copies it)
- `templates[]`: `{id, title, cards[]}`. `id` is the `estimateFor` value.
  `cards[].inputSection.components[]` are the inputs, `cards[].mathsSection`
  the formulas the calculator evaluates.
- Group services (`subType: "subServiceSelector"`, e.g. S3, DynamoDB):
  `templates` is a list of sub-service codes and `templateId` is the group's
  `estimateFor`.
- `mappingDefinitions[]`: price files the calculator loads, relative to
  calculator.aws (`pricing/2.0/meteredUnitMaps/...`). For `PLC_2.0` mappings
  (`.../{mapping}/metadata.json`), the sibling
  `{Location}/primary-selector-aggregations.json` lists every column combination
  a `columnFormIPM` table can be priced with, e.g. `{"Compute Family": "m5.large",
  "vCPU": "2", ...}`. `{Location}` is the pricing region name, URL-encoded:
  `US East (N. Virginia)`, `EU (Ireland)` (not "Europe"), `AWS GovCloud (US)`.

## In-progress estimate (browser)

The calculator keeps the estimate being edited in the tab's `sessionStorage`,
key `awspc-root-estimate-v1`, value `{"estimate": <estimate shape below>}`.
Writing it and reloading `https://calculator.aws/#/estimate` opens it editable
and prices every service (status, cost and `configSummary` are written back to
the same key; `status` is `"priceUpdated"` when the stored `serviceCost` object
differed, even by an extra `"upfront": 0`). Switching a tab to `#/estimate` first
stores a fresh empty estimate, so write the key again after the switch.
`calc_build.py --js` generates that snippet. Nothing leaves the browser until
someone clicks Share.

## Save API

Request headers: `Content-Type: application/json`, `Origin:
https://calculator.aws`, `Referer: https://calculator.aws/`. Body: the estimate.

Response: `{"statusCode": 200, "body": "{\"savedKey\": \"<40 hex>\"}"}`.
`body` is a JSON string. Saved estimates are public to anyone with the key,
immutable, and (as of 2026-11) do not expire.

## Estimate shape

```json
{
  "name": "...",
  "services": {},
  "groups": {
    "grp-<uuid>": {
      "name": "...",
      "services": {
        "<serviceCode>-<uuid>": {
          "serviceCode": "...", "region": "us-east-1", "estimateFor": "<template id>",
          "version": "<definition version>", "description": null,
          "calculationComponents": {"<component id>": {"value": "...", "unit": "..."}},
          "serviceCost": {"monthly": 0, "upfront": 0},
          "serviceName": "...", "regionName": "...", "configSummary": "..."
        }
      },
      "groups": {}, "groupSubtotal": {}, "totalCost": {"monthly": 0, "upfront": 0}
    }
  },
  "groupSubtotal": {}, "totalCost": {"monthly": 0, "upfront": 0}, "support": {},
  "metaData": {"locale": "en_US", "currency": "USD", "createdOn": "...", "source": "calculator-platform"}
}
```

A group service entry replaces `calculationComponents` with
`"subServices": [<service entries>]` and uses the group's `templateId` as
`estimateFor`. The stored copy comes back identical, plus
`metaData.estimateId`.

## Saved value shapes by input type

From the calculator's load ("rehydrate") code:

| subType | Saved entry |
|---|---|
| numericInput, percentInput | `{"value": "<number>"}` (percent: 0-100) |
| fileSize | `{"value": "<n>", "unit": "<size>\|<freq>"}`, freq `NA` when the input has no frequency list |
| frequency | `{"value": "<n>", "unit": "<option id>"}` (e.g. `perMonth`) |
| throughput | `{"value": "<n>", "unit": "<option value>"}` (e.g. `mbps`) |
| durationInput | `{"value": "<n>", "unit": "<option value>"}` (e.g. `hr`, `hours`) |
| dropdown | `{"value": "<option id>"}` (string, exact) |
| radioTiles | `{"value": "<option value>"}` |
| checkbox | `{"value": true}` |
| dataTransferV2 | `{"value": [{"entryType": "INBOUND"\|"OUTBOUND"\|"INTRA_REGION", "value": "<n>", "unit": "...", "fromRegion"/"toRegion": "..."}]}` |
| columnFormIPM | `{"value": [{"<selectorId>": {"value": "..."}, ...}]}` (one row); a utilization column has no selectorId and is saved as `"undefined": {"value": {"unit": "100", "selectedId": "%Utilized/Month"}}` |
| ec2AdvPricingStrategyV2 | on-demand `{"selectedOption": "on-demand", "utilizationValue": "100", "utilizationUnit": "%Utilized/Month"}`; commitments `{"selectedOption", "term": "1 Year"\|"3 Year", "upfrontPayment": "None"\|"Partial"\|"All", "model"}`; spot `{"selectedOption": "spot", "spotDiscountPercentage": "60"}` |

# Troubleshooting

Run `python scripts/calc_validate.py estimate.json` first. It catches most of
these before saving.

## Link total differs from the calculator after "Update estimate"

**Cause:** the shared link displays the saved `serviceCost` values. "Update
estimate" recomputes each service from `calculationComponents`, and services
whose saved cost was off get a **"Cost updated"** badge.

**Fix:** SKILL.md Step 5. Load the estimate in the browser before saving
(`calc_build.py --js`), read the calculator's numbers back
(`scripts/browser_readback.js`), copy them into `monthlyCost`, rebuild, then save.

## A service shows $0.00 or a cost far too low

Usually an input the calculator couldn't use:
- **Input left out.** Optional inputs missing from `calculationComponents`
  load as 0. A missing `storageType` gives $0 storage. `calc_build.py` lists
  these as `not set, so the calculator uses 0`.
- **Unit not accepted.** E.g. fileSize `"unit": "gb"` instead of `"gb|NA"`.
  The value is dropped.
- **Dropdown value not an option id.** It falls back to the default option, which
  may be priced at 0 or differently.
- **Field from another template.** E.g. MSK Serverless fields inside an MSK
  Provisioned entry. They are ignored. The validator names the right template.

## A service costs far too much

Check units and multipliers. Secrets Manager `secretDuration` "730" without
`"unit": "hr"` was read as 730 days: $19.52 instead of $0.86. Dropdowns like
"requests units: millions" multiply the number next to them.

## Read-only warning

**Symptom:** blue banner "One or more services in your estimate are not
compatible with the latest calculator release(s) and are available as
read-only", and the service status says Read-only.

**Cause:** the calculator threw while loading that service's components. One
example is EC2 `pricingStrategy.selectedOption: "onDemand"`, which must be
`"on-demand"`. Read-only services keep their saved cost and can't be edited.
`version` mismatches do **not** cause this: the loader doesn't compare versions.

**Fix:** run the validator. If it passes and the service is still read-only,
configure that service once in the calculator UI, share it, and copy the
components with `calc_fetch.py`.

## "Required input" status

A required input is missing, or a table row (`columnFormIPM`) was rejected.
The validator reports both. A rejected row means a cell value isn't in the
calculator's pricing data: MSK `"Compute Family": "kafka.m5.large"` (must be
`"m5.large"`), OpenSearch `"Instance Family": "Standard Instances - Current
Generation"` (must be `"Memory optimized"`). The service's edit page shows
"Invalid Selection. '<value>' is not supported with the other selected inputs."
For conditional inputs, look at the `visible if:` lines from `calc_discover.py`.

## A table service costs more than expected

A table input left out isn't 0: the calculator prices its default row.
OpenSearch without `columnFormIPM_2` / `columnFormIPM` gets 3 r5.2xlarge
masters and 2 UltraWarm nodes (~$3,900/mo extra). Keep those tables with
`"Number of ..."` set to `"0"`. The validator warns about missing tables.

## A service's edit page and the estimate page disagree

`browser_readback.js` lists these under `mismatches`. It is the calculator, not the
format: the same components give two totals. Known case: OpenSearch gp3 storage
under 300 GB per node (100 GB: $311.96 on the estimate page, $295.96 on the edit
page). Shared links show the estimate-page figure; report both.

## Claude in Chrome: "timed out after 45000ms"

`javascript_tool` stops waiting after 45 s. Start `browser_readback.js` without
`await` and poll `window.__calcReadback` (SKILL.md Step 5). Keep each poll's wait
around 20 s. The read-back keeps running in the page after a timeout.

## Claude in Chrome: result cut off ("[TRUNCATED]")

Single strings are cut at 1,000 characters. Fetch `window.__calcReadback.result.lines`
in slices (`.slice(0, 15)`): an array of short strings comes back whole.

## The loaded estimate shows as an empty "My Estimate"

Switching to `#/estimate` makes the calculator store a fresh estimate. The `load.js`
from `calc_build.py --js` writes the estimate again after the switch; if an older
loader was used, run it again from the `#/estimate` page.

## Save API errors

`calc_save.py` stops with the raw response if the API returns a non-200
`statusCode`, a non-JSON body or no `savedKey`. It never prints a URL built from
an error message. Missing
Origin/Referer headers return a CORS error (`calc_utils.curl_post` sets them).

## The calculator opens a different estimate than the link

The calculator restores its last in-progress estimate from the tab's
`sessionStorage` (`awspc-root-estimate-v1`) and drops `?id=`. Open the link in a
new tab or private window.

## Service code not found (HTTP 403)

The definition CDN answers 403 for unknown codes. Find the right code with
`calc_discover.py --list <keyword>`. Codes are case-sensitive. Non-obvious ones:
EC2 `ec2Enhancement`, EBS `amazonElasticBlockStore`, S3
`amazonSimpleStorageServiceGroup` (not the inactive `amazonS3`), DynamoDB
`amazonDynamoDb`, Lambda `aWSLambda`, Flink `amazonKinesisDataAnalytics`,
OpenSearch `amazonElasticsearchService`, AppStream `amazonAppStream`.

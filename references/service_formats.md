# Service Formats

Each block is a complete **spec service entry** for `calc_build.py`: paste it
into a group's `"services"` list and change the values. `version`,
`estimateFor` and `serviceName` are filled in by the builder.

All blocks are **browser-verified** (2026-11-08): loaded into calculator.aws
(`calc_build.py --js`), priced by the calculator with no "Required input",
"Invalid Selection" or read-only status. Monthly cost in us-east-1:

| Service | serviceCode | Calculator cost |
|---|---|---|
| EC2 (t3.medium, 30 GB gp3) | `ec2Enhancement` | $32.77 |
| EBS (100 GB gp3) | `amazonElasticBlockStore` | $8.00 |
| S3 Standard (group) | `amazonSimpleStorageServiceGroup` | $2.39 |
| Secrets Manager | `awsSecretsManager` | $0.86 |
| Managed Apache Flink | `amazonKinesisDataAnalytics` | $421.53 |
| AppStream 2.0 | `amazonAppStream` | $337.26 |
| RDS for Oracle | `amazonRdsForOracle` | $1,039.48 |
| MSK Provisioned | `amazonManagedStreamingForApacheKafkaMsk` | $489.90 |
| OpenSearch | `amazonElasticsearchService` | $311.96 (edit page: $295.96, see below) |

For anything else: `calc_discover.py <code> --skeleton spec.json` (table inputs
included), or configure one instance by hand in the calculator and copy its
`calculationComponents` (SKILL.md, "Capture a format from the UI").

## Format rules that broke the old examples

| Input type | Wrong | Right |
|---|---|---|
| fileSize | `"unit": "gb"` | `"unit": "gb\|NA"`, or `"gb\|month"` where the unit list says so |
| durationInput | no unit | `"unit": "hr"` / `"day"` / `"hours"` (from the unit list) |
| dropdown | label (`"gp3"`) or number (`1000000`) | option id as a string (`"Storage General Purpose gp3 GB Mo"`, `"1000000"`) |
| EC2 pricing strategy | `"onDemand"` | `"on-demand"` plus `utilizationValue` / `utilizationUnit` |
| any input | left out | spelled out: an optional input left out loads as **0**, not as its default |
| table input (`columnFormIPM`) | left out | spelled out: a missing table gets the calculator's **default row** (OpenSearch: 3 masters, 2 UltraWarm nodes), not 0 |
| table cell (instance type, family, edition) | API names (`kafka.m5.large`), guessed labels (`Standard Edition Two`) | values from the calculator's pricing data (`m5.large`, `Standard Two`); the validator lists them |
| fields from another template | mixed into one entry | separate service entry with that `estimateFor` |

---

## EC2

1x t3.medium Linux On-Demand, 30 GB gp3 → $32.77/mo in us-east-1.

```json
{
  "serviceCode": "ec2Enhancement",
  "region": "us-east-1",
  "calculationComponents": {
    "tenancy": {"value": "shared"},
    "selectedOS": {"value": "linux"},
    "workloadSelection": {"value": "consistent"},
    "workload": {"value": {"workloadType": "consistent", "data": "1"}},
    "instanceType": {"value": "t3.medium"},
    "pricingStrategy": {"value": {"selectedOption": "on-demand", "utilizationValue": "100", "utilizationUnit": "%Utilized/Month"}},
    "storageType": {"value": "Storage General Purpose gp3 GB Mo"},
    "storageAmount": {"value": "30", "unit": "gb|NA"},
    "gp3Iops": {"value": "3000"},
    "gp3Throughput": {"value": "125", "unit": "mbps"},
    "snapshotFrequency": {"value": "0"},
    "detailedMonitoringCheckbox": {"value": false},
    "ec2AdvancedPricingMetrics": {"value": 1},
    "dataTransferForEC2": {"value": []},
    "aditionalCost": {"value": "0"}
  }
}
```

- `workload.value.data` is the instance count.
- `selectedOS`: `linux`, `windows`, `rhel`, `suse`, ... (see `calc_discover.py ec2Enhancement`).
- Part-time instances: `utilizationUnit` is `%Utilized/Month` (0-100),
  `Hours/Day` (0-24), `Hours/Week` (0-168) or `Hours/Month` (0-730).
- Commitments and Spot (`pricingStrategy.value`, captured from the UI, then
  loaded back and priced: t3.medium Compute SP 1yr No Upfront = $21.90/mo):

  | Strategy | value |
  |---|---|
  | Compute Savings Plans | `{"selectedOption": "compute-savings", "term": "1 Year", "upfrontPayment": "None", "model": "computeSavings"}` |
  | EC2 Instance Savings Plans | `{"selectedOption": "instance-savings", "term": "3 Year", "upfrontPayment": "All", "model": "instanceSavings"}` |
  | Standard Reserved | `{"selectedOption": "standard", "term": "1 Year", "upfrontPayment": "Partial", "model": "standard"}` |
  | Convertible Reserved | `{"selectedOption": "convertible", "term": "3 Year", "upfrontPayment": "None", "model": "convertible"}` |
  | Spot | `{"selectedOption": "spot", "spotDiscountPercentage": "60"}` |

  `term`: `"1 Year"` / `"3 Year"`. `upfrontPayment`: `"None"` / `"Partial"` / `"All"`.
  Upfront amounts go into the service's `upfrontCost` (Standard RI 1yr Partial
  for t3.medium: $109 upfront + $9.05/mo).

---

## EBS

1 volume, 100 GB gp3, baseline IOPS/throughput, no snapshots → $8.00/mo.

```json
{
  "serviceCode": "amazonElasticBlockStore",
  "region": "us-east-1",
  "calculationComponents": {
    "numberOfInstances": {"value": "1"},
    "durationOfInstanceRuns": {"value": "730", "unit": "hours"},
    "storageType": {"value": "Storage General Purpose gp3 GB Mo"},
    "storageAmount": {"value": "100", "unit": "gb|NA"},
    "gp3Iops": {"value": "3000"},
    "gp3Throughput": {"value": "125", "unit": "mbps"},
    "snapshotFrequency": {"value": "0"}
  }
}
```

- io1: `"storageType": {"value": "Storage Provisioned IOPS GB Mo"}` plus `"iops": {"value": "3000"}` (required for io1).
- The calculator UI defaults `snapshotFrequency` to `"59.83"` (2x daily). Set it on purpose.

---

## S3

S3 is a **group** service: the configuration lives in `subServices`.
100 GB S3 Standard, 10k PUT, 100k GET → $2.39/mo.

```json
{
  "serviceCode": "amazonSimpleStorageServiceGroup",
  "region": "us-east-1",
  "subServices": [
    {
      "serviceCode": "amazonS3Standard",
      "calculationComponents": {
        "s3StandardStorageSize": {"value": "100", "unit": "gb|month"},
        "moveToStorageClassMethod": {"value": "No movement required"},
        "s3StandardPutRequests": {"value": "10000"},
        "s3StandardGetRequests": {"value": "100000"},
        "s3StandardDataReturnedSize": {"value": "0", "unit": "gb|month"},
        "s3StandardDataScannedSize": {"value": "0", "unit": "gb|month"}
      }
    }
  ]
}
```

- Other storage classes are sibling sub-services (`amazonS3StandardInfrequentAccess`,
  `amazonS3IntelligentTiering`, `amazonS3Glacier`, ...). `calc_discover.py amazonSimpleStorageServiceGroup` lists them.
- The old `amazonS3` service code is **inactive** and no longer loads.
- DynamoDB (`amazonDynamoDb`) is a group service too.

---

## Secrets Manager

2 secrets for the full month, 10k API calls → $0.86/mo.

```json
{
  "serviceCode": "awsSecretsManager",
  "region": "us-east-1",
  "calculationComponents": {
    "NumberOfSecrets": {"value": "2"},
    "secretDuration": {"value": "730", "unit": "hr"},
    "numberOfAPIs": {"value": "10000", "unit": "perMonth"}
  }
}
```

Without `"unit"` the 730 was read as days and the service cost $19.52.

---

## Managed Apache Flink

```json
{
  "serviceCode": "amazonKinesisDataAnalytics",
  "region": "us-east-1",
  "calculationComponents": {
    "numberOfFlinkApps": {"value": "1"},
    "numberOfFlinkKPU": {"value": "4", "unit": "perHour"},
    "numberOfBackups": {"value": "1"},
    "appBackupSize": {"value": "1", "unit": "gb|NA"},
    "numberOfStudioApps": {"value": "0"},
    "numberOfStudioKPU": {"value": "0", "unit": "perHour"}
  }
}
```

---

## AppStream 2.0

Always-on fleet, 7 users on stream.standard.xlarge Windows multi-session.

```json
{
  "serviceCode": "amazonAppStream",
  "region": "us-east-1",
  "calculationComponents": {
    "appStreamFleet_instancePriceModel": {"value": "730"},
    "appStream_numberOfWorkingHours": {"value": "24"},
    "percentBuffer": {"value": "0"},
    "appStream_instanceDiskVolumeSizeGB": {"value": "200"},
    "columnFormIPM": {
      "value": [
        {
          "Instance Type": {"value": "stream.standard.xlarge"},
          "Operating System": {"value": "Windows"},
          "Multi Session": {"value": "True"}
        }
      ]
    },
    "appStream_licenseModel": {"value": "User Fees Windows AppStream provided per users"},
    "numberPicker": {"value": "0"},
    "appStream_numberOfUsers": {"value": "7"},
    "appStream_numberofsession": {"value": "7"},
    "daysInWD": {"value": "5"},
    "appStreamFleet_utilization1": {"value": "24"},
    "daysInWE": {"value": "2"},
    "appStreamFleet_utilization2": {"value": "24"},
    "baselineUsersPerHourWD": {"value": "7"},
    "peakUsersPerHourWD": {"value": "7"},
    "baselineUsersPerHourWE": {"value": "7"},
    "peakUsersPerHourWE": {"value": "7"}
  }
}
```

---

## RDS for Oracle

1x db.m5.xlarge Single-AZ, SE2 license included, 800 GB io1 with 3000 IOPS.

```json
{
  "serviceCode": "amazonRdsForOracle",
  "region": "us-east-1",
  "calculationComponents": {
    "columnFormIPM": {
      "value": [
        {
          "Number of Nodes": {"value": "1"},
          "Instance Type": {"value": "db.m5.xlarge"},
          "undefined": {"value": {"unit": "100", "selectedId": "%Utilized/Month"}},
          "Deployment Option": {"value": "Single-AZ"},
          "TermType": {"value": "OnDemand"},
          "License Model": {"value": "License included"},
          "Database Edition": {"value": "Standard Two"}
        }
      ]
    },
    "storageType": {"value": "Provisioned IOPS"},
    "storageAmount": {"value": "800", "unit": "gb|NA"},
    "provisioningIOPS": {"value": "3000"},
    "DatabaseInsightsSelected": {"value": "0"},
    "retentionPeriod": {"value": "0"}
  }
}
```

`"undefined"` is the real column key: the utilization column has no selector id.

---

## MSK Provisioned

3x m5.large brokers, 100 GB per broker.

```json
{
  "serviceCode": "amazonManagedStreamingForApacheKafkaMsk",
  "region": "us-east-1",
  "calculationComponents": {
    "columnFormIPM": {
      "value": [
        {
          "Number of Kafka broker nodes": {"value": "3"},
          "Compute Family": {"value": "m5.large"}
        }
      ]
    },
    "msk_storageAmount": {"value": "100", "unit": "gb|NA"},
    "numberOfPrivateConnHours": {"value": "0"},
    "dataProcessedForPrivateConn": {"value": "0", "unit": "gb|month"},
    "dataTransfer": {"value": []}
  }
}
```

MSK Serverless (`awsMSKServerless`) and MSK Connect (`awsMSKConnect`) are
separate templates: add another service entry with `"estimateFor"` set and
discover their fields. The old example mixed their fields into this one, and
the calculator dropped them.

---

## OpenSearch

2x r5.large.search data nodes, no dedicated masters, no UltraWarm, 100 GB gp3 each.
Captured from the calculator UI. Keep the master and UltraWarm rows with 0
nodes: left out, the calculator prices its defaults (3 masters, 2 UltraWarm).

```json
{
  "serviceCode": "amazonElasticsearchService",
  "region": "us-east-1",
  "calculationComponents": {
    "columnFormIPM_1": {
      "value": [
        {
          "Number of Nodes Data instance": {"value": "2"},
          "Instance Type": {"value": "r5.large.search"},
          "undefined": {"value": {"unit": "100", "selectedId": "%Utilized/Month"}},
          "Instance Family": {"value": "Memory optimized"},
          "TermType": {"value": "OnDemand"},
          "Storage": {"value": "EBS Only"}
        }
      ]
    },
    "columnFormIPM_2": {
      "value": [
        {
          "Number of Nodes Dedicated master": {"value": "0"},
          "Instance Type": {"value": "r5.large.search"},
          "undefined": {"value": {"unit": "100", "selectedId": "%Utilized/Month"}},
          "Instance Family": {"value": "Memory optimized"},
          "TermType": {"value": "OnDemand"},
          "Storage": {"value": "EBS Only"}
        }
      ]
    },
    "columnFormIPM": {
      "value": [
        {
          "Number of Nodes": {"value": "0"},
          "Instance Type": {"value": "ultrawarm1.medium.search"},
          "undefined": {"value": {"unit": "100", "selectedId": "%Utilized/Month"}},
          "TermType": {"value": "OnDemand"}
        }
      ]
    },
    "numberOfInstances": {"value": "2"},
    "storageType": {"value": "GP3"},
    "gp3StorageAmount": {"value": "100", "unit": "gb|NA"},
    "gp3ProvisioningIOPS": {"value": "3000"},
    "gp3Throughput": {"value": "125", "unit": "mbps"}
  }
}
```

Calculator quirk: the estimate page and the service's edit page price small gp3
volumes differently (same components, also when the UI wrote them):

| gp3 per node (2 × r5.large.search) | Estimate page | Edit page |
|---|---|---|
| 0 GB | $287.56 | $319.56 |
| 100 GB | $311.96 | $295.96 (= list price: $271.56 + 200 GB × $0.122) |
| 300 GB | $344.76 | $344.76 |

The data nodes are $271.56 in every row, so the gap is in how the two pages apply
gp3's included IOPS/throughput to small volumes. Shared links show the estimate-page
figure. `browser_readback.js` reports such gaps under `mismatches`.

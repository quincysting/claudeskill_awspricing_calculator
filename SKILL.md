---
name: aws-pricing-calculator
description: >
  Generate shareable AWS Pricing Calculator (calculator.aws) URLs from
  architecture descriptions, blog posts, solution documents or sizing CSVs.
  Use for "AWS cost estimate", "pricing calculator", "calculator URL",
  "cost this architecture", "estimate for this blog post", "MAP estimate".
  Discovers live service schemas, builds and validates the estimate JSON,
  saves it, and reads back the calculator's own numbers.
---

# AWS Pricing Calculator Skill

Scripts live in `~/.claude/skills/aws-pricing-calculator/scripts/` (below: `$S`).
Python 3 stdlib + curl only.

## How the calculator treats a saved estimate

Know this before building anything. Each point was confirmed in the browser.

1. **The shared link first shows the saved `serviceCost` values.** These are
   whatever the skill wrote, right or wrong.
2. **"Update estimate"** (needed to edit) **recomputes every service from
   `calculationComponents`** with the calculator's own prices. A service whose
   saved cost was off gets a **"Cost updated"** badge and the new number.
3. If a service's components can't be loaded (bad enum value, wrong shape), it
   becomes **Read-only** and keeps its saved cost. You'll see the blue "not
   compatible with the latest calculator" banner.
4. Inputs are unforgiving:
   - A dropdown value that isn't an option id **silently falls back** to the default option.
   - A unit that isn't a unit id **drops the value**.
   - An optional input left out loads as **0**, not as its UI default. A missing
     table input (`columnFormIPM`) is the exception: it gets the **default row**
     (OpenSearch: 3 dedicated masters + 2 UltraWarm nodes).
   - A table cell (instance type, family, edition) that isn't in the calculator's
     pricing data is rejected ("Invalid Selection"): another value is priced, or
     none ("Required input").
   - Fields belonging to another template are ignored.
5. The estimate being edited lives in the tab's `sessionStorage`
   (`awspc-root-estimate-v1`). Writing an estimate there and reloading makes the
   calculator price it **without saving or sharing anything**. Steps 5 and 2.3 use this.

So accuracy comes from **correct `calculationComponents`** (enforced by
`calc_validate.py`, including table rows checked against the pricing data). The
link and the calculator agree only when `monthlyCost` holds **the calculator's
numbers**, which is what Steps 5 and 6 do.

## Workflow

### Step 1: Extract the architecture and write down assumptions

From the doc or description, list each service with region (default
`us-east-1`), instance types, counts, hours/utilization, storage, requests and
data transfer. Write these as an **assumptions table**; it goes into the final
answer. If the source leaves a cost driver unstated (hours/day, GB/month,
request volume), pick a stated assumption. Don't silently use a default.

### Step 2: Get the exact input formats

1. Check `references/service_formats.md` for a ready block (9 services, all
   browser-verified, plus EC2 Savings Plans / Reserved / Spot strategies).
2. Otherwise:
   ```bash
   python $S/calc_discover.py --list <keyword>              # find service codes
   python $S/calc_discover.py <code> [<code> ...]           # ids, option ids, unit ids, visibility
   python $S/calc_discover.py <code> ... --skeleton spec.json   # spec with every visible input at its UI default
   ```
   The skeleton marks the EC2 instance type `SET-INSTANCE-TYPE`, and validation
   fails until you replace it. Table inputs get the calculator's default row,
   completed from the pricing data; change the counts and instance types, and set
   unused tables (masters, UltraWarm) to 0 nodes rather than deleting them.
   Inputs printed as "no documented format" (some uncommon subTypes) aren't in
   the skeleton. Get those from a UI capture (3. below).
   Group services (S3 `amazonSimpleStorageServiceGroup`, DynamoDB `amazonDynamoDb`)
   list their sub-services. Discover those codes and put them under `subServices`.
3. **Capture a format from the UI** when unsure: the format the UI writes is the
   reference. With a browser tool, open calculator.aws, load any estimate that
   contains the service (Step 5 loader) or add the service by hand, configure it
   on its edit page, click the page's **Update** button (local only), then read
   the components back:
   `JSON.parse(sessionStorage.getItem('awspc-root-estimate-v1')).estimate`.
   Without a browser tool, ask the user to configure it and share the link, then
   `python $S/calc_fetch.py "<url>" --spec ui_spec.json`.

### Step 3: Fill the spec

Edit `spec.json` (format in `calc_build.py` docstring). Rules:
- Use the **ids** printed after `value ids:` / `unit ids:`, as strings, exactly.
- Spell out every input that matters. Missing optional inputs become 0.
- Watch multiplier dropdowns and unit fields. "5 million requests" is either
  `{"value": "5", "unit": "millionPerMonth"}` or `5000000` with `perMonth`.
- Review UI defaults you keep. Example: EBS `snapshotFrequency` defaults to 2x daily.
- Don't set `version`, `estimateFor` (unless picking a non-default template) or
  `serviceName`; the builder fills them from the live definition.
- `monthlyCost` / `upfrontCost`: your expected cost, used until Step 6 (`--costs`) replaces it.
  Get unit prices from the AWS Pricing MCP (`get_pricing`) when available.

### Step 4: Build and validate

```bash
python $S/calc_build.py spec.json -o estimate.json --js load.js
```
Fix every `[ERROR]` (each one names the valid ids or table values). Read the
`[WARNING]`/`[NOTE]` lines: "uses 0" lists inputs you left out, "default row"
lists tables the calculator will fill in. Re-run until it prints `OK`.

### Step 5: Price it in the calculator before saving (calibrate)

Use the browser tool available in the session. Nothing is published in this step.

1. Open a **new** tab at `https://calculator.aws/#/`. Don't reuse a tab with the
   user's own work: the loader replaces its in-progress estimate.
2. Run the contents of `load.js` in that page. It stores the estimate as the tab's
   in-progress estimate and reloads; the calculator prices every service.
3. Start the read-back without waiting for it (it opens each service's edit page,
   about 7 s each, and page-script tools often stop waiting after about 45 s):
   `(<contents of scripts/browser_readback.js>)(); 'started'`
   Poll until `status` is `done`:
   `await new Promise(r => setTimeout(r, 20000)); JSON.stringify({s: window.__calcReadback.status, n: window.__calcReadback.checked, e: window.__calcReadback.error})`
   (A tool that waits for the function's promise with no time limit gets the result directly.)
   If a poll finds `window.__calcReadback` undefined, the page reloaded: start step 3 again.
4. Check `window.__calcReadback.result`: `readOnlyBanner`, and the `Status` column in
   `root` / `groups`:
   - `Read-only`: format bug. Fix the component, rebuild, back to 2.
   - `Required input`: a field is missing or a table row was rejected. Open the
     service's edit page: it names the field ("Invalid Selection ..."). Fix, rebuild, back to 2.
   - A service more than ~10% away from your Step 3 expectation: check units,
     multipliers and counts before trusting either number.
5. Copy `window.__calcReadback.result.lines` into `readback.jsonl`, one line per
   array item, fetching `.slice(0, 15)`, `.slice(15, 30)`, ... (some tools cut long
   strings; each line is short and is valid JSON on its own).
6. Close the tab.

`result.mismatches` lists services whose edit page shows a different total than the
estimate page: a calculator inconsistency (OpenSearch gp3 storage under 300 GB per
node does this). Shared links show the estimate-page figure; report both.

Without a browser tool, skip this step and tell the user the link opens with the
skill's estimates and that "Update estimate" shows the calculator's figures.

### Step 6: Save

```bash
python $S/calc_save.py estimate.json --costs readback.jsonl   # after Step 5
python $S/calc_save.py estimate.json                          # no browser tool
```
`--costs` copies the calculator's own cost and config summary into each service (by
service id, exactly as the calculator stores them, so the link opens with no "Cost
updated" badges), refuses to save if the calculator couldn't price a service, and
prints the mismatches. Then it validates, saves, prints the URL and confirms the
stored copy matches. The URL is public to anyone who has it; say so if the content is
sensitive. Saved estimates can't be deleted, so save once.

### Step 7: Report

Give the user:
1. The calculator URL
2. A table: service, configuration, monthly, upfront, and the source of each
   number (calculator read-back, or skill estimate if Step 5 couldn't run)
3. Monthly, upfront and 12-month totals
4. The assumptions table, plus any service left unverified or read-only

## Tools

| Script | Purpose |
|---|---|
| `calc_discover.py` | List services; show inputs, option/unit ids, visibility; `--skeleton` (tables included) |
| `calc_build.py` | Spec → estimate JSON, auto-fills version/template, validates; `--js` browser loader |
| `calc_validate.py` | Validate an estimate JSON against live definitions and table pricing data |
| `calc_save.py` | Apply calculator costs (`--costs`), validate, save, round-trip check, print URL |
| `calc_fetch.py` | Download a saved estimate; `--spec` turns it into an editable spec |
| `browser_readback.js` | Page function: calculator-computed status/cost per service, edit-page cross-check, `lines` for `--costs` |
| `calc_utils.py` | Endpoints, curl helpers, caching |

Editing an existing estimate: `calc_fetch.py <url> --spec spec.json`, edit,
build, save. Saved estimates are immutable, so this produces a new URL.

## References

| File | Contents |
|---|---|
| `references/service_formats.md` | Ready spec blocks and the format rules that break estimates |
| `references/troubleshooting.md` | Symptom → cause → fix |
| `references/api_endpoints.md` | Endpoints and payload shapes |

// Read back what the AWS Pricing Calculator itself computes for an estimate.
//
// Works on either:
//   - an estimate loaded locally with the `calc_build.py --js` loader (preferred: nothing is
//     published), or
//   - a saved link https://calculator.aws/#/estimate?id=<key>, opened in a new tab.
// Evaluate this function in the page with your browser tool. Tools that stop waiting after
// about 45 s: start it without awaiting,
//   (<this function>)(); 'started'
// then poll (each call under 45 s) until status is "done":
//   await new Promise(r => setTimeout(r, 30000)); JSON.stringify(window.__calcReadback)
// window.__calcReadback = {status: "running"|"done"|"error", checked, result, error}.
// Then copy window.__calcReadback.result.lines (a few at a time, e.g. .slice(0, 15)) into
// readback.jsonl, one per line, for `calc_save.py estimate.json --costs readback.jsonl`.
// Option: (<this function>)({crossCheck: false}) skips opening each edit page (about 7 s per service).
//
// Returns:
//   mode            "local" (in-progress estimate) or "shared" (saved link, read-only until updated)
//   firstView       total shown on open (shared: the saved serviceCost values)
//   recomputed      total after recalculation (= the calculator's own math)
//   readOnlyBanner  true if any service failed to load
//   root / groups   per-row "Service Name", "Status", "Monthly cost", "Upfront cost", "Config Summary"
//                   Status: "-" ok, "Cost updated" (saved cost was off), "Read-only" (format broken),
//                   "Required input" (missing field or a table row the calculator rejected)
//   priced          {<service id>: {monthly, upfront, status, configSummary, subServices}} as the
//                   calculator stored them; save it to a file for `calc_save.py --costs`
//   mismatches      services whose edit page total differs from the estimate page
//                   (a calculator inconsistency; report both numbers)
//
// It only clicks "Update estimate", group names and edit buttons (leaving each edit page
// without saving). It never saves, shares or accepts dialogs.
async ({ crossCheck = true } = {}) => {
  const state = window.__calcReadback = { status: 'running', checked: 0 };
  try {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const text = () => document.body.innerText;
  const num = s => (s == null ? null : parseFloat(String(s).replace(/[^\d.-]/g, '')));
  for (let i = 0; i < 30 && !/Update estimate|Add service/.test(text()); i++) await sleep(1000);
  const total = () => (text().match(/Monthly cost\n([\d,.]+) USD/) || [])[1];
  const firstView = total();
  const update = [...document.querySelectorAll('button')].find(b => b.innerText.trim() === 'Update estimate');
  update?.click();
  // wait at least 5 s, then until the total has been unchanged for 3 s (pricing loads per service), max 60 s
  for (let i = 0, last = null, same = 0; i < 60 && (i < 5 || same < 3); i++) {
    await sleep(1000);
    const t = total();
    same = t && t === last ? same + 1 : 0;
    last = t;
  }
  const rows = () => [...document.querySelectorAll('tbody tr')];
  const table = () => {
    const head = [...document.querySelectorAll('thead th')].map(th => th.innerText.trim());
    return rows().map(r => [...r.querySelectorAll('td')].map(td => td.innerText.trim()))
      .filter(c => c.length === head.length)
      .map(c => Object.fromEntries(head.map((h, i) => [h || 'col' + i, c[i]])));
  };
  const openGroup = async (name, sel = '[role=treeitem], [role=tab], li, a, button') => {
    const el = [...document.querySelectorAll(sel)]
      .find(e => !e.closest('table') && e.innerText?.trim() === name);
    if (el) { el.click(); await sleep(2000); }
    return !!el;
  };
  // #/estimate reopens the last group shown; the top level opens from the breadcrumb link
  let estName = null;
  try { estName = JSON.parse(sessionStorage.getItem('awspc-root-estimate-v1')).estimate.name; } catch (e) { /* none */ }
  const openView = view => (view === estName ? openGroup(view, '[class*=breadcrumb] a') : openGroup(view));
  if (estName) await openView(estName);
  const root = table();
  const groups = {};
  for (const name of root.map(r => r['Service Name'])) if (await openGroup(name)) groups[name] = table();

  const mismatches = [];
  if (crossCheck) {
    const home = async () => { location.hash = '#/estimate'; await sleep(2500); };
    for (const view of [estName, ...Object.keys(groups)].filter(Boolean)) {
      await home();
      if (!(await openView(view))) continue;
      const count = rows().length;
      for (let i = 0; i < count; i++) {
        const row = rows()[i];
        const btn = row?.querySelector('button[aria-label^="edit "]');
        const head = [...document.querySelectorAll('thead th')].map(th => th.innerText.trim());
        const cells = [...(row?.querySelectorAll('td') || [])].map(td => td.innerText.trim());
        const cell = h => cells[head.indexOf(h)];
        if (!btn || groups[cell('Service Name')]) continue;  // group rows: checked inside the group
        const name = btn.getAttribute('aria-label').slice(5);
        const est = num(cell('Monthly cost'));
        btn.click();
        for (let k = 0; k < 20 && !/Total Monthly cost:/.test(text()); k++) await sleep(500);
        await sleep(3000);
        const edit = num((text().match(/Total Monthly cost:\s*([\d,.]+) USD/) || [])[1]);
        if (est != null && edit != null && Math.abs(est - edit) >= 0.01)
          mismatches.push({ group: view, row: i, service: name, estimatePage: est, editPage: edit });
        state.checked += 1;
        await home();
        await openView(view);
      }
    }
    await home();
  }

  const priced = {};
  const walk = node => {
    for (const [id, s] of Object.entries(node.services || {}))
      priced[id] = { monthly: s.serviceCost?.monthly, upfront: s.serviceCost?.upfront, status: s.status ?? null,
                     configSummary: s.configSummary,
                     subServices: (s.subServices || []).map(x => ({ monthly: x.serviceCost?.monthly, upfront: x.serviceCost?.upfront,
                                                                    configSummary: x.configSummary })) };
    Object.values(node.groups || {}).forEach(walk);
  };
  try { walk(JSON.parse(sessionStorage.getItem('awspc-root-estimate-v1')).estimate); } catch (e) { /* no local copy */ }

  // The same data as JSON lines short enough for tools that cut long strings (some at 1000 chars):
  // write them one per line to readback.jsonl for `calc_save.py --costs readback.jsonl`.
  const lines = [];
  for (const [id, p] of Object.entries(priced)) {
    lines.push(JSON.stringify({ id, monthly: p.monthly, upfront: p.upfront, status: p.status,
                                sub: p.subServices.map(s => [s.monthly, s.upfront]) }));
    for (const [sub, txt] of [[null, p.configSummary], ...p.subServices.map((s, i) => [i, s.configSummary])])
      for (let k = 0; txt && k < txt.length; k += 500) lines.push(JSON.stringify({ id, sub, summary: txt.slice(k, k + 500) }));
  }
  mismatches.forEach(m => lines.push(JSON.stringify({ mismatch: m })));

  const result = { mode: update ? 'shared' : 'local', firstView, recomputed: total(),
                   readOnlyBanner: /not compatible with the latest calculator/.test(text()), root, groups, priced, mismatches, lines };
  Object.assign(state, { status: 'done', result });
  return result;
  } catch (e) {
    Object.assign(state, { status: 'error', error: String(e) });
    throw e;
  }
}

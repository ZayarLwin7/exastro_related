/* Drives the page's real grid logic under a stub DOM.
   The point: the seeding rules are client-side, and every other test can only
   look for strings. This one runs the code and checks what the form would send,
   which is the only place the "blank means what?" question can be answered.
   The harness is fed by tests/test_exastro_automate.py, which splices the live
   script out of the rendered page -- so this cannot drift from what ships. */
const out = [];
const ok = (what) => { out.push("OK " + what); };
const bad = (what, got) => { throw new Error("FAIL " + what + " -> " + JSON.stringify(got)); };
const flush = async (n) => { for (let i = 0; i < (n || 6); i++) { await new Promise(r => setImmediate(r)); } };

const timers = [];
const answers = [];
/* A re-run link and a rejected form both arrive with the name already in the
   box, so the read has to happen without anyone touching anything. That case
   cannot be reached by firing events later in this file -- the script runs its
   load-time seed while it is being evaluated -- so it is simulated at the stub. */
const preset = process.env.DSH_PRESET_SHEET || "";
const presetParams = process.env.DSH_PRESET_PARAMS || "";
const urls = [];
globalThis.setTimeout = (fn) => { timers.push(fn); return 0; };
globalThis.clearTimeout = () => {};
const runTimers = () => { while (timers.length) { timers.shift()(); } };

const els = {};
function mk(id, extra) {
  const e = {
    id, value: "", textContent: "", innerHTML: "", style: {}, dataset: {}, _l: {},
    addEventListener(t, f) { (this._l[t] = this._l[t] || []).push(f); },
    fire(t, ev) { (this._l[t] || []).forEach(f => f(ev || this)); },
    querySelector() { return null; }, closest() { return null; },
    focus() {}, setSelectionRange() {}, selectionStart: 0,
  };
  e._cls = (extra && extra._cls) || [];
  e.classList = { contains: (c) => e._cls.indexOf(c) >= 0 };
  Object.assign(e, extra || {});
  if (extra && extra._cls) { e._cls = extra._cls; }
  els[id] = e;
  return e;
}
if (preset) {
  answers.push({ state: "ok", columns: {
    p_age: { name: "My Age", required: true, unique: false } } });
}
["parameters", "jsonstatus", "colpreview", "colrows", "column_meta",
 "movement_name", "sheet_name", "sheetstate", "fmt", "active"].forEach(id => mk(id));
const form = mk("form");
if (preset) { els.sheet_name.value = preset; els.sheet_name.dataset.touched = "1"; }
if (presetParams) { els.parameters.value = presetParams; }

globalThis.document = {
  getElementById: (id) => (id in els ? els[id] : null),
  querySelector: () => form,
  activeElement: els.active,
};
globalThis.fetch = (url) => {
  urls.push(url);
  const data = answers.length > 1 ? answers.shift() : answers[0];
  return Promise.resolve({ json: () => Promise.resolve(data) });
};

/* --- the shipped script, with its internals handed back ------------------- */
__T_DECL__
(function () {
__GRID_BODY__
  globalThis.__g = {
    render, seed, diff, shown,
    params: els.parameters, sheet: els.sheet_name, movement: els.movement_name,
    rows: els.colrows, meta: els.column_meta, state: els.sheetstate,
  };
})();
/* ------------------------------------------------------------------------- */

const g = globalThis.__g;
const sent = () => els.column_meta.value || "{}";

if (preset) {
  await flush();
  if (urls[0] !== "/api/sheet/" + preset) {
    bad("a prefilled form did not read its own sheet on load", urls);
  }
  if (g.rows.innerHTML.indexOf('value="My Age"') < 0) {
    bad("the prefilled form showed no seeded label", g.rows.innerHTML.slice(0, 300));
  }
  if (sent() !== "{}") { bad("a form nobody touched reported changes", sent()); }
  console.log("OK a prefilled form reads its sheet on load");
  process.exit(0);            // the cases below start from an empty form
}

// 1. a fresh form knows nothing, and says nothing
g.params.value = JSON.stringify({ p_age: 27, p_name: "x" });
g.render();
if (sent() !== "{}") { bad("an untouched form sends nothing", sent()); }
ok("untouched form sends nothing");

// 2. the name arriving by MIRROR must still read the sheet. This is the case
//    that silently never seeded: assigning `.value` fires no event.
answers.push({ state: "ok", columns: {
  p_age: { name: "My Age", required: true, unique: false },
  p_name: { name: "My Name", required: true, unique: false } } });
g.movement.value = "mvmt_1";
g.movement.fire("input");
if (g.sheet.value !== "mvmt_1") { bad("the rest name did not mirror", g.sheet.value); }
runTimers();
await flush();
if (urls.length !== 1 || urls[0] !== "/api/sheet/mvmt_1") {
  bad("a mirrored name did not ask ITA what it holds", urls);
}
ok("a mirrored sheet name reads the sheet");

// 3. the rows arrive filled, and a filled row is NOT a change
if (g.rows.innerHTML.indexOf('value="My Age"') < 0) {
  bad("the display name was not seeded", g.rows.innerHTML.slice(0, 200));
}
if (!/class="rq"[^>]*checked/.test(g.rows.innerHTML)) { bad("required was not seeded", ""); }
if (sent() !== "{}") { bad("prefilled rows reported a change", sent()); }
ok("seeded labels and flags show, and change nothing");

// 4. clearing a label is the one instruction the old grid could not express
g.rows.fire("input", {
  target: { value: "", dataset: { k: "p_age" }, closest: () => null,
            classList: { contains: (c) => c === "dn" } },
});
if (sent() !== '{"p_age":{"name":""}}') {
  bad("a cleared label did not ask for the logical name", sent());
}
ok("a cleared box means the logical name");

// 5. retyping what the sheet already says is silence again
g.rows.fire("input", {
  target: { value: "My Age", dataset: { k: "p_age" }, closest: () => null,
            classList: { contains: (c) => c === "dn" } },
});
if (sent() !== "{}") { bad("an unchanged label was sent as a change", sent()); }
ok("a label back to its baseline sends nothing");

// 6. un-ticking a prefilled Required is a change, and says false
g.rows.fire("input", {
  target: { checked: false, value: "", dataset: { k: "p_age" }, closest: () => null,
            classList: { contains: (c) => c === "rq" } },
});
if (sent() !== '{"p_age":{"required":false}}') {
  bad("un-ticking a seeded flag did not say false", sent());
}
ok("un-ticking a seeded flag sends false");

// 7. a name that is not there yet, and one that cannot be read, are told apart
//    (fresh keys, because the rows above carry deliberate edits -- those carry
//    over to a sheet you have not moved on from, which is the point of `edits`)
g.params.value = JSON.stringify({ other_key: 1 }); g.render();
answers.length = 0; answers.push({ state: "absent", columns: {} });
g.sheet.value = "brand_new"; g.sheet.fire("change"); await flush();
if (g.rows.innerHTML.indexOf("My Age") >= 0) { bad("an absent sheet kept seeded rows", g.rows.innerHTML); }
if (g.rows.innerHTML.indexOf("other_key") < 0) { bad("the new row was not drawn", g.rows.innerHTML); }
if (els.sheetstate.style.display !== "block") { bad("a new sheet said nothing", ""); }
if (sent() !== "{}") { bad("a new sheet reported changes nobody made", sent()); }
ok("a new sheet is announced as new");

answers.length = 0; answers.push({ state: "error", columns: {} });
g.sheet.value = "unreadable_sheet"; g.sheet.fire("change"); await flush();
if (urls[urls.length - 1] !== "/api/sheet/unreadable_sheet") { bad("no read issued", urls); }
if (sent() !== "{}") { bad("an unreadable sheet invented changes", sent()); }
ok("an unreadable sheet is reported, and stays silent");

// 8. re-reading a sheet that failed before must be allowed, not remembered as a
//    known answer -- an absent sheet is re-fetched, so a typo cannot stick
const before = urls.length;
g.sheet.value = "brand_new"; g.sheet.fire("change"); await flush();
if (urls.length !== before) { ok("an absent sheet is read again"); }
else { bad("a failed read was cached as an answer", urls.slice(before)); }

// 9. clearing the name drops the baseline rather than trusting a stale one
g.sheet.value = ""; g.sheet.fire("change"); runTimers(); await flush();
g.params.value = JSON.stringify({ p_age: 27 }); g.render();
if (g.rows.innerHTML.indexOf('value="My Age"') >= 0) { bad("a stale baseline survived", g.rows.innerHTML); }
ok("an empty name forgets the sheet it had read");

console.log(out.join("\n"));

/* Drives the page's real host-popup logic under a stub DOM.

   Two bugs in this dialog were found by a human clicking it, not by a test:
   an inline style that outranked the class showing it, and a switch that had
   to also clear what it stood for when cancelled. Both are behaviours of this
   script, and no amount of asserting on template strings can see them, so this
   file runs the shipped code and checks what the form would post.

   The script body is spliced in by tests/test_exastro_automate.py from the
   rendered page, so this cannot drift from what ships. */
const out = [];
const ok = (what) => { out.push("OK " + what); };
const bad = (what, got) => { throw new Error("FAIL " + what + " -> " + JSON.stringify(got)); };
const flush = async (n) => { for (let i = 0; i < (n || 8); i++) { await new Promise(r => setImmediate(r)); } };

const calls = [];
const answers = [];
globalThis.fetch = (url) => {
  calls.push(url);
  const body = answers.length > 1 ? answers.shift() : answers[0];
  return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
};

const els = {};
function mk(id) {
  const e = {
    id, value: "", checked: false, textContent: "", style: {}, dataset: {},
    _l: {}, children: [], _html: "",
    addEventListener(t, f) { (this._l[t] = this._l[t] || []).push(f); },
    fire(t, ev) { (this._l[t] || []).forEach(f => f(ev || this)); },
    appendChild(c) { this.children.push(c); return c; },
  };
  const cls = new Set();
  e.classList = {
    add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c),
  };
  Object.defineProperty(e, "innerHTML", {
    get() { return e._html; },
    set(v) { e._html = v; if (v === "") { e.children = []; } },
  });
  els[id] = e;
  return e;
}
["create_op", "hostpick", "host_group", "hostecho", "hp-group",
 "hp-note", "hp-ok", "hp-cancel"].forEach(mk);
// scenario 7 needs a fresh dialog against a different answer, so the module
// state the shipped script keeps is reset the same way a reload would
const groups_reset = () => { els["hp-group"].innerHTML = ""; };

globalThis.document = {
  getElementById: (id) => (id in els ? els[id] : null),
  createElement: (tag) => ({ tag, value: "", textContent: "" }),
};

__T_DECL__

__POPUP_BODY__

/* ---- scenarios ---------------------------------------------------------- */
(async () => {
  // the install sends its own spelling, so the page never hard-codes `[HG]`
  answers.push({ prefix: "[HG]", groups: [
    { name: "DEMO_GP", hosts: ["demo_host_a", "demo_host_b"] },
    { name: "EMPTY_GP", hosts: [] }] });

  // 1. flipping the switch must actually show the dialog
  els.create_op.checked = true;
  els.create_op.fire("change");
  if (!els.hostpick.classList.contains("on")) { bad("the switch opens the dialog"); }
  if (calls[0] !== "/api/host_groups") { bad("the lists come from the install", calls); }
  await flush();
  ok("the switch opens the dialog and asks the install");

  // 2. the label is the value ITA stores; the posted value stays the plain name
  const opts = () => els["hp-group"].children;
  if (opts().map(o => o.value).join(",") !== "DEMO_GP,EMPTY_GP") {
    bad("the form posts plain names", opts().map(o => o.value));
  }
  if (opts().map(o => o.textContent).join(",") !== "[HG]DEMO_GP,[HG]EMPTY_GP") {
    bad("the dropdown shows the stored spelling", opts().map(o => o.textContent));
  }
  ok("options read [HG]group while the form posts the plain name");

  // 3. the count moved into the note, where there is room for a sentence
  if (!/DEMO_GP has 2 host\(s\) linked/.test(els["hp-note"].textContent)) {
    bad("a chosen group reports its membership", els["hp-note"].textContent);
  }
  els["hp-group"].value = "EMPTY_GP";
  els["hp-group"].fire("change");
  if (!/EMPTY_GP.*no hosts linked to it yet/.test(els["hp-note"].textContent)) {
    bad("an empty group is called out, by name", els["hp-note"].textContent);
  }
  els["hp-group"].value = "DEMO_GP";
  els["hp-group"].fire("change");
  if (!/has 2 host/.test(els["hp-note"].textContent)) { bad("and the note follows the choice", els["hp-note"].textContent); }
  ok("the note reports membership and warns about a group at zero");

  // 4. confirming posts the group and closes
  els["hp-group"].value = "DEMO_GP";
  els["hp-ok"].fire("click");
  if (els.host_group.value !== "DEMO_GP") { bad("confirming posts the group", els.host_group.value); }
  if (els.hostpick.classList.contains("on")) { bad("confirming closes the dialog"); }
  if (!/\[HG\]DEMO_GP/.test(els.hostecho.textContent)) { bad("the echo shows what ITA will be sent", els.hostecho.textContent); }
  if (/@@/.test(els.hostecho.textContent)) { bad("no placeholder leaks through", els.hostecho.textContent); }
  ok("confirming posts the group and echoes the value ITA will store");

  // 4b. no group at all is no agreement: it stays open
  els["hp-group"].value = "";
  els.hostpick.classList.add("on");
  els["hp-ok"].fire("click");
  if (!els.hostpick.classList.contains("on")) { bad("no group, no close"); }
  if (els.host_group.value !== "DEMO_GP") { bad("the previous choice survives", els.host_group.value); }
  if (!/Choose a host group/.test(els["hp-note"].textContent)) { bad("and the note says why", els["hp-note"].textContent); }
  ok("confirming with no group keeps the dialog open and says why");

  // 5. cancel stands the whole offer down
  els.create_op.checked = true;
  els["hp-cancel"].fire("click");
  if (els.create_op.checked !== false) { bad("cancel turns the switch off", els.create_op.checked); }
  if (els.host_group.value !== "") { bad("cancel clears what was chosen", els.host_group.value); }
  if (els.hostpick.classList.contains("on")) { bad("cancel closes the dialog"); }
  ok("cancel turns the switch off and clears the choice");

  // 6. switching off by hand clears the field the form posts
  els.host_group.value = "DEMO_GP";
  els.create_op.checked = false;
  els.create_op.fire("change");
  if (els.host_group.value !== "") { bad("switch off clears", els.host_group.value); }
  ok("switching off clears the posted field");

  // 7. an install with no groups says so instead of offering an empty box
  answers.push({ prefix: "[HG]", groups: [] });
  groups_reset();
  els.create_op.checked = true;
  els.create_op.fire("change");
  await flush();
  if (!/host group/.test(els["hp-note"].textContent)) { bad("no groups is stated", els["hp-note"].textContent); }
  ok("no groups on the install is said out loud");

  // 8. the lists are read once, not once per opening
  const before = calls.length;
  groups_reset();
  els.create_op.checked = false; els.create_op.fire("change");
  els.create_op.checked = true; els.create_op.fire("change");
  await flush();
  if (calls.length !== before) { bad("the popup caches its groups", calls.length - before); }
  ok("the popup reads the install once and reopens from memory");

  console.log(out.join("\n"));
})();

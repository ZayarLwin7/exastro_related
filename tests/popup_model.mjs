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
  answers.push({ groups: [{ name: "DEMO_GP", hosts: ["demo_host_a", "demo_host_b"] },
                          { name: "EMPTY_GP", hosts: [] }] });

  // 1. flipping the switch must actually show the dialog
  els.create_op.checked = true;
  els.create_op.fire("change");
  if (!els.hostpick.classList.contains("on")) { bad("the switch opens the dialog"); }
  if (calls[0] !== "/api/host_groups") { bad("the lists come from the install", calls); }
  await flush();
  ok("the switch opens the dialog and asks the install");

  // 2. one dropdown, listing groups, with the membership count beside each name
  const opts = () => els["hp-group"].children;
  if (opts().map(o => o.value).join(",") !== "DEMO_GP,EMPTY_GP") {
    bad("groups are listed as returned", opts().map(o => o.value));
  }
  if (opts()[0].textContent !== "DEMO_GP (2)") {
    bad("a group shows how many hosts it has", opts()[0].textContent);
  }
  if (opts()[1].textContent !== "EMPTY_GP (0)") {
    bad("including when it has none", opts()[1].textContent);
  }
  ok("each group is labelled with its own host count");

  // 3. the note explains what choosing a group means
  if (!/whole group/.test(els["hp-note"].textContent)) { bad("the note explains the scope", els["hp-note"].textContent); }
  els["hp-group"].value = "EMPTY_GP";
  els["hp-group"].fire("change");
  if (!/whole group/.test(els["hp-note"].textContent)) { bad("and stays after a change", els["hp-note"].textContent); }
  ok("the note says the run goes to every host in the group");

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
  answers.push({ groups: [] });
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

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
["create_op", "hostpick", "host_group", "host_name", "hostecho", "hp-group",
 "hp-host", "hp-note", "hp-ok", "hp-cancel"].forEach(mk);

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
  if (!els.hostpick.classList.contains("on")) { bad("the switch opens the dialog", els.hostpick._cls); }
  ok("the switch opens the dialog");

  if (calls[0] !== "/api/host_groups") { bad("the lists come from the install", calls); }
  await flush();
  const groups = els["hp-group"].children.map(c => c.value);
  if (groups.join(",") !== "DEMO_GP,EMPTY_GP") { bad("groups are listed", groups); }
  ok("groups are listed as the install reports them");

  // 2. the host dropdown follows the group
  const hostsOf = () => els["hp-host"].children.map(c => c.value);
  if (hostsOf().join(",") !== "demo_host_a,demo_host_b") { bad("hosts follow the group", hostsOf()); }
  els["hp-group"].value = "EMPTY_GP";
  els["hp-group"].fire("change");
  if (hostsOf().length !== 0) { bad("an empty group offers no hosts", hostsOf()); }
  if (!/No hosts are linked/.test(els["hp-note"].textContent)) { bad("and says why", els["hp-note"].textContent); }
  ok("a group with no hosts offers none and says so");

  // 3. confirming writes both hidden fields, and the echo tells the operator
  els["hp-group"].value = "DEMO_GP";
  els["hp-group"].fire("change");
  els["hp-host"].value = "demo_host_b";
  els["hp-ok"].fire("click");
  if (els.host_group.value !== "DEMO_GP" || els.host_name.value !== "demo_host_b") {
    bad("confirming posts group and host", [els.host_group.value, els.host_name.value]);
  }
  if (els.hostpick.classList.contains("on")) { bad("confirming closes the dialog"); }
  if (!/demo_host_b/.test(els.hostecho.textContent)) { bad("the form echoes the choice", els.hostecho.textContent); }
  ok("confirming posts the group and the host and closes");

  // 3b. without a host there is no agreement: reopened, and it stays open
  els.hostpick.classList.add("on");
  els["hp-host"].value = "";
  els["hp-ok"].fire("click");
  if (!els.hostpick.classList.contains("on")) { bad("no host, no close"); }
  if (els.host_name.value !== "demo_host_b") { bad("the previous choice is untouched", els.host_name.value); }
  if (!/Choose a host/.test(els["hp-note"].textContent)) { bad("and the note explains", els["hp-note"].textContent); }
  ok("confirming with no host keeps the dialog open");

  // 4. cancel means "do not create these"
  els.hostpick.classList.add("on");
  els.create_op.checked = true;
  els["hp-cancel"].fire("click");
  if (els.create_op.checked !== false) { bad("cancel turns the switch off", els.create_op.checked); }
  if (els.host_group.value !== "" || els.host_name.value !== "") {
    bad("cancel clears what was chosen", [els.host_group.value, els.host_name.value]);
  }
  if (els.hostpick.classList.contains("on")) { bad("cancel closes the dialog"); }
  ok("cancel turns the switch off and clears the choice");

  // 5. switching off by hand also clears it, so the form cannot post a request
  //    to create an input row with no host behind it
  els.host_group.value = "DEMO_GP"; els.host_name.value = "demo_host_a";
  els.create_op.checked = false;
  els.create_op.fire("change");
  if (els.host_group.value !== "" || els.host_name.value !== "") { bad("switch off clears", [els.host_group.value, els.host_name.value]); }
  ok("switching off clears the posted fields");

  // 6. the lists are fetched once, not once per opening
  const before = calls.length;
  els.create_op.checked = true;
  els.create_op.fire("change");
  await flush();
  if (calls.length !== before) { bad("the popup caches its lists", calls.length - before); }
  ok("the popup reads the install once and reopens from memory");

  console.log(out.join("\n"));
})();

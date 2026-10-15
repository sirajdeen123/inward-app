// Live tally of ticked Despatch rows against the outbound Ship document (the server re-checks on save).
(function () {
  "use strict";
  var form = document.getElementById("tally-form");
  if (!form) { return; }
  function milli(text) {               // decimal text -> integer thousandths, avoiding float rounding drift
    var v = String(text || "").trim();
    if (!v) { return null; }
    var neg = v.charAt(0) === "-";
    if (neg) { v = v.slice(1); }
    var parts = v.split(".");
    var frac = ((parts[1] || "") + "000").slice(0, 4);
    var n = parseInt(parts[0] || "0", 10) * 1000 + parseInt(frac.slice(0, 3), 10) + (parseInt(frac.charAt(3), 10) >= 5 ? 1 : 0);
    return neg ? -n : n;
  }
  function show(n) {
    var s = (n / 1000).toFixed(3).replace(/\.?0+$/, "");
    return s === "" ? "0" : s;
  }
  var target = { ltr: milli(form.dataset.targetLtr), kg: milli(form.dataset.targetKg), nop: milli(form.dataset.targetNop) };
  var save = document.getElementById("t-save");
  var status = document.getElementById("t-status");
  function update() {
    var sum = { ltr: 0, kg: 0, nop: 0 };
    form.querySelectorAll("input.tick:checked").forEach(function (box) {
      sum.ltr += milli(box.dataset.ltr) || 0;
      sum.kg += milli(box.dataset.kg) || 0;
      sum.nop += milli(box.dataset.nop) || 0;
    });
    document.getElementById("t-ltr").textContent = show(sum.ltr);
    document.getElementById("t-kg").textContent = show(sum.kg);
    document.getElementById("t-nop").textContent = show(sum.nop);
    var ok = sum.ltr === target.ltr && sum.kg === target.kg && (target.nop === null || sum.nop === target.nop);
    if (save && !save.hasAttribute("data-blocked")) { save.disabled = !ok; }
    status.textContent = ok ? "Matches - save" : "Not matching";
    status.className = "chip " + (ok ? "chip-ok" : "chip-warn");
  }
  if (save && save.disabled) { save.setAttribute("data-blocked", "1"); }
  form.addEventListener("change", update);
  update();
}());

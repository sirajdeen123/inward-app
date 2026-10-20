// Outbound manual rows: running LTR / KG total of the typed rows against the Ship document (the server re-checks).
(function () {
  "use strict";
  var out = document.getElementById("mt-total");
  if (!out) { return; }
  var form = out.closest("form");
  function num(v) { var n = parseFloat(String(v || "").replace(/,/g, "")); return isNaN(n) ? 0 : n; }
  function fam(u) { u = String(u || "L").trim().toUpperCase(); return (u === "K" || u === "KG" || u === "G") ? "KG" : "LTR"; }
  function show(n) { var s = n.toFixed(3).replace(/\.?0+$/, ""); return s === "" ? "0" : s; }
  function update() {
    var ltr = 0, kg = 0;
    form.querySelectorAll("input[name^='qty_']").forEach(function (q) {
      var i = q.name.slice(4), u = form.querySelector("input[name='uom_" + i + "']");
      if (fam(u && u.value) === "KG") { kg += num(q.value); } else { ltr += num(q.value); }
    });
    var tl = num(out.dataset.ltr), tk = num(out.dataset.kg);
    var ok = Math.abs(ltr - tl) < 0.0005 && Math.abs(kg - tk) < 0.0005;
    out.textContent = "Typed rows: LTR " + show(ltr) + " of " + show(tl) + " · KG " + show(kg) + " of " + show(tk) +
      (ok ? " - tallies" : " - not matching yet");
    out.className = ok ? "ok-text" : "muted";
  }
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  update();
}());

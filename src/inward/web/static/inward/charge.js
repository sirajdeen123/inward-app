// Unloading charge screen: shows tonnage x rate as the clerk types (the server still calculates the saved charge).
(function () {
  "use strict";
  var input = document.getElementById("u-ton"), out = document.getElementById("u-amount");
  if (!input || !out) { return; }
  var rate = parseFloat(input.getAttribute("data-rate"));
  if (!(rate > 0)) { return; }
  function money(n) { return n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function show() {
    var t = parseFloat((input.value || "").replace(/,/g, ""));
    out.textContent = "";
    if (!(t > 0)) { out.textContent = "-"; return; }
    out.appendChild(document.createTextNode("Rs " + money(Math.round(t * rate * 100) / 100) + " "));
    var small = document.createElement("small");
    small.textContent = t + " t × Rs " + money(rate);
    out.appendChild(small);
  }
  input.addEventListener("input", show);
}());

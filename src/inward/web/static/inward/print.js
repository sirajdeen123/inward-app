// Print button on printable pages (inline scripts are blocked by the security policy).
(function () {
  "use strict";
  var btn = document.getElementById("print-btn");
  if (btn) { btn.addEventListener("click", function () { window.print(); }); }
}());

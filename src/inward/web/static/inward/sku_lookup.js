// SKU boxes in manual-entry tables (outbound Ship rows, vendor challan rows): type part of the full SKU, the Floor
// SKU (XG9 991 / XG9991) or a word of the description and pick from the packaging master. Picking fills the
// description and UOM of the same row (only empty boxes); with data-autoqty the quantity follows pieces x pack size.
// The server still checks everything on save; without this script the clerk types the rows as before.
(function () {
  "use strict";
  var inputs = document.querySelectorAll("input[data-sku-lookup]");
  if (!inputs.length || !window.fetch) { return; }
  var url = inputs[0].getAttribute("data-sku-lookup");
  var list = document.createElement("datalist");
  list.id = "sku-master";
  document.body.appendChild(list);
  var known = {}, timer = null, last = "";

  function field(row, prefix) { return row.querySelector("input[name^='" + prefix + "_']"); }

  function fillQty(row) {
    var sku = field(row, "sku"), nop = field(row, "nop"), qty = field(row, "qty");
    var it = sku && known[sku.value];
    if (!it || !it.pack || !nop || !qty || !sku.hasAttribute("data-autoqty") || qty.getAttribute("data-typed")) { return; }
    var n = parseFloat((nop.value || "").replace(/,/g, ""));
    if (n > 0) { qty.value = String(Math.round(n * parseFloat(it.pack) * 1000) / 1000); }
  }

  function fill(input) {
    var it = known[input.value], row = input.closest("tr");
    var note = row && row.querySelector(".sku-note");
    if (note) { note.textContent = it ? it.floor + " · " + it.description : ""; }
    if (!it || !row) { return; }
    var desc = field(row, "desc"), uom = field(row, "uom");
    if (desc && !desc.value) { desc.value = it.description; }
    if (uom && !uom.value && it.uom) { uom.value = it.uom; }
    fillQty(row);
  }

  function show(items) {
    list.textContent = "";
    items.forEach(function (it) {
      known[it.sku] = it;
      var o = document.createElement("option");
      o.value = it.sku;
      o.label = it.floor + " · " + it.description;
      o.textContent = o.label;
      list.appendChild(o);
    });
  }

  Array.prototype.forEach.call(inputs, function (input) {
    input.setAttribute("list", "sku-master");
    input.setAttribute("autocomplete", "off");
    input.addEventListener("input", function () {
      var pos = input.selectionStart;
      input.value = input.value.toUpperCase();
      try { input.setSelectionRange(pos, pos); } catch (e) { /* ignore */ }
      if (known[input.value]) { fill(input); return; }
      var q = input.value.trim();
      if (q.length < 2 || q === last) { return; }
      clearTimeout(timer);
      timer = setTimeout(function () {
        last = q;
        fetch(url + "?q=" + encodeURIComponent(q), { credentials: "same-origin" })
          .then(function (r) { return r.ok ? r.json() : { items: [] }; })
          .then(function (d) { show(d.items || []); if (known[input.value]) { fill(input); } })
          .catch(function () { /* offline: the full code can still be typed */ });
      }, 220);
    });
    input.addEventListener("change", function () { fill(input); });
    var row = input.closest("tr");
    var nop = row && field(row, "nop"), qty = row && field(row, "qty");
    if (nop) { nop.addEventListener("input", function () { fillQty(row); }); }
    if (qty) { qty.addEventListener("input", function () { qty.setAttribute("data-typed", qty.value ? "1" : ""); }); }
  });
}());

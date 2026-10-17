// Excess SKU search: suggestions from the SKU packaging list while typing; the code is kept in capitals.
(function () {
  var input = document.getElementById("x-sku");
  if (!input) return;
  var list = document.getElementById("sku-options");
  var desc = document.getElementById("x-desc");
  var url = input.getAttribute("data-search");
  var timer = null, last = "";

  function show(items) {
    list.innerHTML = "";
    items.forEach(function (it) {
      var o = document.createElement("option");
      o.value = it.sku;
      o.textContent = it.floor + " · " + it.description;
      list.appendChild(o);
    });
    var exact = items.filter(function (it) { return it.sku === input.value; })[0];
    desc.textContent = exact ? exact.description : (input.value.length === 16 ? "Not in the packaging list - add it below." : "");
  }

  input.addEventListener("input", function () {
    var pos = input.selectionStart;
    input.value = input.value.toUpperCase();
    try { input.setSelectionRange(pos, pos); } catch (e) { /* ignore */ }
    var q = input.value.trim();
    if (q.length < 3 || q === last) return;
    clearTimeout(timer);
    timer = setTimeout(function () {
      last = q;
      fetch(url + "?q=" + encodeURIComponent(q), {credentials: "same-origin"})
        .then(function (r) { return r.ok ? r.json() : {items: []}; })
        .then(function (d) { show(d.items || []); })
        .catch(function () { /* offline: the clerk can still type the full code */ });
    }, 250);
  });
})();

// Organisation search box (outbound destination, 21-Oct-2026): type part of the code or the name and pick.
// Ranking: exact code, code starting with the text, a word of the name starting with it, then anywhere in the
// name; destinations of the uploaded Despatch report come first. Arrow keys + Enter, or tap. The server still
// checks the value on save; without this script the clerk types "code : name" as before.
(function () {
  "use strict";
  var inputs = document.querySelectorAll("input[data-org-pick]");
  Array.prototype.forEach.call(inputs, function (input) {
    var src = document.getElementById(input.getAttribute("data-org-pick"));
    if (!src) { return; }
    var orgs = JSON.parse(src.textContent || "[]");
    var box = document.createElement("ul");
    box.className = "pick-list";
    box.setAttribute("role", "listbox");
    box.hidden = true;
    input.parentNode.classList.add("pick-wrap");
    input.parentNode.appendChild(box);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    var items = [], active = -1;

    function score(o, q) {
      var code = o.c.toUpperCase(), name = o.n.toUpperCase(), s = -1;
      if (code === q) { s = 100; }
      else if (code.indexOf(q) === 0) { s = 80; }
      else if (name.indexOf(q) === 0) { s = 70; }
      else if (new RegExp("[^A-Z0-9]" + q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).test(name)) { s = 60; }
      else if (name.indexOf(q) >= 0 || (code + " " + name).indexOf(q) >= 0) { s = 40; }
      return s < 0 ? s : s + (o.r ? 10 : 0);
    }
    function pick(o) {
      input.value = o.c + " : " + o.n;
      close();
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }
    function close() { box.hidden = true; active = -1; input.setAttribute("aria-expanded", "false"); }
    function mark() {
      Array.prototype.forEach.call(box.children, function (li, i) { li.classList.toggle("on", i === active); });
    }
    function render() {
      var q = input.value.split(" : ")[0].trim().toUpperCase().replace(/^'/, "");
      if (input.value.indexOf(" : ") > 0) { q = input.value.trim().toUpperCase(); }
      box.textContent = "";
      if (!q) { items = orgs.filter(function (o) { return o.r; }); }
      else {
        items = orgs.map(function (o) { return [score(o, q), o]; })
          .filter(function (x) { return x[0] >= 0; })
          .sort(function (a, b) { return b[0] - a[0] || (a[1].c < b[1].c ? -1 : 1); })
          .slice(0, 12).map(function (x) { return x[1]; });
      }
      if (!items.length) {
        if (q) { var li = document.createElement("li"); li.className = "muted"; li.textContent = "No match - type it as 'code : name' to add a new depot"; box.appendChild(li); box.hidden = false; }
        else { close(); }
        return;
      }
      items.forEach(function (o, i) {
        var li = document.createElement("li");
        li.setAttribute("role", "option");
        var b = document.createElement("b"); b.textContent = o.c;
        li.appendChild(b);
        li.appendChild(document.createTextNode(" · " + o.n));
        if (o.r) { var t = document.createElement("span"); t.className = "chip chip-ok"; t.textContent = "in report"; li.appendChild(document.createTextNode(" ")); li.appendChild(t); }
        li.addEventListener("mousedown", function (e) { e.preventDefault(); pick(o); });
        box.appendChild(li);
        if (i === 0) { active = 0; }
      });
      mark();
      box.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }
    input.addEventListener("input", render);
    input.addEventListener("focus", function () { if (!input.value) { render(); } });
    input.addEventListener("blur", close);
    input.addEventListener("keydown", function (e) {
      if (box.hidden || !items.length) { return; }
      if (e.key === "ArrowDown") { active = Math.min(items.length - 1, active + 1); mark(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { active = Math.max(0, active - 1); mark(); e.preventDefault(); }
      else if (e.key === "Enter" && active >= 0) { pick(items[active]); e.preventDefault(); }
      else if (e.key === "Escape") { close(); }
    });
  });
}());

// IW form: add a new source organisation or add e-mails to one, without leaving the form (UAT 18-Oct-2026).
(function () {
  "use strict";
  var panel = document.getElementById("org-add");
  if (!panel || !window.fetch) { return; }
  var form = panel.closest("form"), button = document.getElementById("org-save"), msg = document.getElementById("org-msg");
  var source = form.querySelector("input[name='source_org']"), list = document.getElementById("org-list");

  function val(id) { var el = document.getElementById(id); return el ? el.value : ""; }

  // typing a known code in the panel shows its current name
  document.getElementById("org-code").addEventListener("change", function () {
    var code = val("org-code").trim().toUpperCase();
    Array.prototype.forEach.call(list.options, function (o) {
      if (o.value.split(" : ")[0] === code && !val("org-name")) {
        document.getElementById("org-name").value = o.value.split(" : ").slice(1).join(" : ");
      }
    });
  });

  button.addEventListener("click", function () {
    var data = new FormData();
    data.append("csrfmiddlewaretoken", form.querySelector("input[name='csrfmiddlewaretoken']").value);
    data.append("code", val("org-code"));
    data.append("name", val("org-name"));
    data.append("org_type", val("org-type"));
    data.append("emails", val("org-emails"));
    button.disabled = true;
    msg.className = "muted";
    msg.textContent = "Saving...";
    fetch(button.getAttribute("data-url"), { method: "POST", body: data, credentials: "same-origin",
                                             headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        button.disabled = false;
        if (d.error) { msg.className = "warn-text"; msg.textContent = d.error; return; }
        var exists = Array.prototype.some.call(list.options, function (o) { return o.value === d.value; });
        if (!exists) { var o = document.createElement("option"); o.value = d.value; list.appendChild(o); }
        if (source) { source.value = d.value; }
        msg.className = "ok-text";
        msg.textContent = d.message;
      })
      .catch(function () { button.disabled = false; msg.className = "warn-text"; msg.textContent = "Not saved - check the connection and try again."; });
  });
}());

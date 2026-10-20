// IW form: add a new source organisation or add e-mails to one, without leaving the form (UAT 18-Oct-2026).
(function () {
  "use strict";
  var panel = document.getElementById("org-add");
  if (!panel || !window.fetch) { return; }
  var form = panel.closest("form"), button = document.getElementById("org-save"), msg = document.getElementById("org-msg");
  var source = form.querySelector("input[name='source_org']"), list = document.getElementById("org-list");

  function val(id) { var el = document.getElementById(id); return el ? el.value : ""; }
  function known(code) {               // the datalist option of an organisation code, or null
    var hit = null;
    Array.prototype.forEach.call(list.options, function (o) { if (o.value.split(" : ")[0] === code) { hit = o; } });
    return hit;
  }

  // 21-Oct-2026: the source's MRA e-mail can be typed only while the organisation has none
  var mail = form.querySelector("input[name='source_email']"), note = document.getElementById("src-mail-note");
  function sourceMail() {
    if (!mail || !source) { return; }
    var o = known(source.value.split(":")[0].trim().toUpperCase());
    var onFile = o && o.getAttribute("data-mra");
    mail.disabled = !!onFile;
    if (onFile) { mail.value = ""; }
    if (note) { note.textContent = onFile ? "MRA e-mail on file: " + onFile + " - not needed." : (o ? "No MRA e-mail yet for this source - enter one." : ""); }
  }
  if (source) { source.addEventListener("input", sourceMail); source.addEventListener("change", sourceMail); sourceMail(); }

  function panelState() {
    var code = val("org-code").trim().toUpperCase(), o = code && known(code);
    var onFile = o && o.getAttribute("data-mra");
    document.getElementById("org-emails").disabled = !!onFile;
    button.disabled = !!onFile;
    msg.className = "muted";
    msg.textContent = onFile ? o.value + " is already in the masters with MRA e-mail " + onFile + "."
      : (o ? o.value + " is in the masters without an MRA e-mail - add it here." : "Used at once; the CFA Manager verifies it.");
  }
  document.getElementById("org-code").addEventListener("input", panelState);

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
        var o = known(d.value.split(" : ")[0]);
        if (!o) { o = document.createElement("option"); o.value = d.value; list.appendChild(o); }
        if (d.mra) { o.setAttribute("data-mra", d.mra); }
        if (source) { source.value = d.value; sourceMail(); }
        msg.className = "ok-text";
        msg.textContent = d.message;
      })
      .catch(function () { button.disabled = false; msg.className = "warn-text"; msg.textContent = "Not saved - check the connection and try again."; });
  });
}());

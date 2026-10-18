// Sign-in screen: show / hide the password.
(function () {
  "use strict";
  var eye = document.getElementById("pw-eye"), pw = document.getElementById("id_password");
  if (!eye || !pw) { return; }
  eye.addEventListener("click", function () {
    var show = pw.type === "password";
    pw.type = show ? "text" : "password";
    eye.setAttribute("aria-label", show ? "Hide password" : "Show password");
    eye.classList.toggle("on", show);
    pw.focus();
  });
}());

// Photo / scan uploads from a phone (forms with class "photo-form").
// A camera photo is 4-8 MB; on mobile data that upload often stalls and the page seems to do nothing.
// So: show what was picked, shrink photos in the browser (long side 2000 px, JPEG) before sending, show the
// progress and say clearly when it failed. PDFs are sent as they are. Without this script the forms still
// post normally.
(function () {
  "use strict";
  var MAX_PX = 2000, QUALITY = 0.82, TIMEOUT_MS = 180000;

  function status(form, text, bad) {
    var el = form.querySelector(".pick-status");
    if (!el) {
      el = document.createElement("span");
      el.className = "pick-status muted";
      form.appendChild(el);
    }
    el.textContent = text;
    el.classList.toggle("warn-text", !!bad);
  }

  function shrink(file) {
    return new Promise(function (resolve) {
      if (!/^image\/(jpeg|png|webp)$/i.test(file.type)) { resolve(file); return; }
      var url = URL.createObjectURL(file), img = new Image();
      img.onload = function () {
        var scale = Math.min(1, MAX_PX / Math.max(img.naturalWidth, img.naturalHeight));
        var canvas = document.createElement("canvas");
        canvas.width = Math.round(img.naturalWidth * scale);
        canvas.height = Math.round(img.naturalHeight * scale);
        canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(url);
        canvas.toBlob(function (blob) {
          if (!blob || blob.size >= file.size) { resolve(file); return; }
          var name = (file.name || "photo").replace(/\.[^.]*$/, "") + ".jpg";
          resolve(new File([blob], name, { type: "image/jpeg" }));
        }, "image/jpeg", QUALITY);
      };
      img.onerror = function () { URL.revokeObjectURL(url); resolve(file); };
      img.src = url;
    });
  }

  function setup(form) {
    var input = form.querySelector("input[type=file]");
    var button = form.querySelector("button[type=submit]");
    if (!input || !button || !window.FormData || !window.Promise) { return; }
    var label = button.textContent;

    input.addEventListener("change", function () {
      var n = input.files.length;
      status(form, n ? n + (n === 1 ? " file" : " files") + " ready - tap " + label : "");
    });

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (!input.files.length) { status(form, "Take a photo or choose a file first.", true); return; }
      button.disabled = true;
      button.textContent = "Preparing...";
      Promise.all(Array.prototype.map.call(input.files, shrink)).then(function (files) {
        var data = new FormData();
        Array.prototype.forEach.call(form.elements, function (el) {
          if (el.name && el.type !== "file" && el.type !== "submit") { data.append(el.name, el.value); }
        });
        files.forEach(function (f) { data.append(input.name, f, f.name); });
        var xhr = new XMLHttpRequest();
        xhr.open("POST", form.action);
        xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
        xhr.timeout = TIMEOUT_MS;
        xhr.upload.onprogress = function (e) {
          if (e.lengthComputable) { button.textContent = "Uploading " + Math.round(e.loaded * 100 / e.total) + "%"; }
        };
        function failed(why) {
          button.disabled = false;
          button.textContent = label;
          status(form, "Not uploaded (" + why + "). Check the signal and tap " + label + " again.", true);
        }
        xhr.onload = function () {
          var next = null;
          try { next = JSON.parse(xhr.responseText).redirect; } catch (e) { next = null; }
          if (xhr.status === 200 && next) { button.textContent = "Saved"; window.location.href = next; }
          else if (xhr.status === 200 && xhr.responseURL) { window.location.href = xhr.responseURL; }
          else { failed("server said " + xhr.status); }
        };
        xhr.onerror = function () { failed("network error"); };
        xhr.ontimeout = function () { failed("took too long"); };
        xhr.send(data);
      });
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll("form.photo-form"), setup);
}());

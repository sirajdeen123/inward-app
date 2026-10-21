// Photo / scan uploads from a phone (forms with class "photo-form").
// A camera photo is 4-8 MB; on mobile data that upload often stalls and the page seems to do nothing.
// So: show what was picked, shrink photos in the browser (long side 2000 px, JPEG) before sending, show the
// progress and say clearly when it failed. PDFs are sent as they are. Without this script the forms still
// post normally.
// Forms with data-crop (signed MRA, LR) offer a crop box after each photo is picked: drag the corners to the
// edges of the paper, then "Use cropped" or "Use whole photo" (UAT 18-Oct-2026).
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

  function isPhoto(file) { return /^image\/(jpeg|png|webp)$/i.test(file.type); }

  // crop: {x, y, w, h} as fractions of the photo (0..1), or null for the whole photo
  function shrink(file, crop) {
    return new Promise(function (resolve) {
      if (!isPhoto(file)) { resolve(file); return; }
      var url = URL.createObjectURL(file), img = new Image();
      img.onload = function () {
        var sx = 0, sy = 0, sw = img.naturalWidth, sh = img.naturalHeight;
        if (crop) {
          sx = Math.round(crop.x * sw); sy = Math.round(crop.y * sh);
          sw = Math.max(1, Math.round(crop.w * img.naturalWidth)); sh = Math.max(1, Math.round(crop.h * img.naturalHeight));
        }
        var scale = Math.min(1, MAX_PX / Math.max(sw, sh));
        var canvas = document.createElement("canvas");
        canvas.width = Math.round(sw * scale);
        canvas.height = Math.round(sh * scale);
        canvas.getContext("2d").drawImage(img, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(url);
        canvas.toBlob(function (blob) {
          if (!blob || (!crop && blob.size >= file.size)) { resolve(file); return; }
          var name = (file.name || "photo").replace(/\.[^.]*$/, "") + ".jpg";
          resolve(new File([blob], name, { type: "image/jpeg" }));
        }, "image/jpeg", QUALITY);
      };
      img.onerror = function () { URL.revokeObjectURL(url); resolve(file); };
      img.src = url;
    });
  }

  // ---------------------------------------------------------------- crop box
  function cropper(file) {
    return new Promise(function (resolve) {
      var url = URL.createObjectURL(file);
      var overlay = document.createElement("div");
      overlay.className = "crop-overlay";
      var stage = document.createElement("div");
      stage.className = "crop-stage";
      var img = document.createElement("img");
      img.alt = "Photo to crop";
      var box = document.createElement("div");
      box.className = "crop-box";
      ["nw", "ne", "sw", "se"].forEach(function (c) {
        var h = document.createElement("span");
        h.className = "crop-handle crop-" + c;
        h.setAttribute("data-corner", c);
        box.appendChild(h);
      });
      var bar = document.createElement("div");
      bar.className = "crop-bar";
      var hint = document.createElement("span");
      hint.textContent = "Drag the corners to the edges of the paper.";
      var useCrop = document.createElement("button");
      useCrop.type = "button"; useCrop.className = "btn btn-primary"; useCrop.textContent = "Use cropped";
      var useAll = document.createElement("button");
      useAll.type = "button"; useAll.className = "btn"; useAll.textContent = "Use whole photo";
      bar.appendChild(hint); bar.appendChild(useAll); bar.appendChild(useCrop);
      stage.appendChild(img); stage.appendChild(box);
      overlay.appendChild(stage); overlay.appendChild(bar);
      document.body.appendChild(overlay);

      var r = { x: 0.05, y: 0.05, w: 0.9, h: 0.9 }, MIN = 0.08;
      function draw() {
        box.style.left = (r.x * img.clientWidth) + "px";
        box.style.top = (r.y * img.clientHeight) + "px";
        box.style.width = (r.w * img.clientWidth) + "px";
        box.style.height = (r.h * img.clientHeight) + "px";
      }
      img.onload = draw;
      window.addEventListener("resize", draw);
      img.src = url;

      var drag = null;
      function point(e) {
        var rect = img.getBoundingClientRect();
        return { x: Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width)),
                 y: Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height)) };
      }
      box.addEventListener("pointerdown", function (e) {
        e.preventDefault();
        box.setPointerCapture(e.pointerId);
        drag = { corner: e.target.getAttribute("data-corner"), start: point(e), r: { x: r.x, y: r.y, w: r.w, h: r.h } };
      });
      box.addEventListener("pointermove", function (e) {
        if (!drag) { return; }
        var p = point(e), o = drag.r, dx = p.x - drag.start.x, dy = p.y - drag.start.y;
        var x1 = o.x, y1 = o.y, x2 = o.x + o.w, y2 = o.y + o.h;
        if (!drag.corner) {                       // move the whole box
          var nx = Math.min(1 - o.w, Math.max(0, o.x + dx)), ny = Math.min(1 - o.h, Math.max(0, o.y + dy));
          r = { x: nx, y: ny, w: o.w, h: o.h };
        } else {
          if (drag.corner.indexOf("w") >= 0) { x1 = Math.min(x2 - MIN, Math.max(0, x1 + dx)); }
          if (drag.corner.indexOf("e") >= 0) { x2 = Math.max(x1 + MIN, Math.min(1, x2 + dx)); }
          if (drag.corner.indexOf("n") >= 0) { y1 = Math.min(y2 - MIN, Math.max(0, y1 + dy)); }
          if (drag.corner.indexOf("s") >= 0) { y2 = Math.max(y1 + MIN, Math.min(1, y2 + dy)); }
          r = { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
        }
        draw();
      });
      box.addEventListener("pointerup", function () { drag = null; });
      box.addEventListener("pointercancel", function () { drag = null; });

      function done(result) {
        window.removeEventListener("resize", draw);
        URL.revokeObjectURL(url);
        overlay.remove();
        resolve(result);
      }
      useCrop.addEventListener("click", function () { done({ x: r.x, y: r.y, w: r.w, h: r.h }); });
      useAll.addEventListener("click", function () { done(null); });
    });
  }

  function setup(form) {
    var input = form.querySelector("input[type=file]");
    var button = form.querySelector("button[type=submit]");
    if (!input || !button || !window.FormData || !window.Promise) { return; }
    var label = button.textContent;
    var crops = [];   // per selected file: crop fractions or null

    input.addEventListener("change", function () {
      var files = Array.prototype.slice.call(input.files), n = files.length;
      crops = files.map(function () { return null; });
      status(form, n ? n + (n === 1 ? " file" : " files") + " ready - tap " + label : "");
      if (!form.hasAttribute("data-crop") || !window.PointerEvent) { return; }
      files.reduce(function (chain, f, i) {       // one photo after the other
        return chain.then(function () {
          if (!isPhoto(f)) { return null; }
          return cropper(f).then(function (c) { crops[i] = c; });
        });
      }, Promise.resolve()).then(function () {
        var cut = crops.filter(Boolean).length;
        status(form, n + (n === 1 ? " file" : " files") + (cut ? " (" + cut + " cropped)" : "") + " ready - tap " + label);
      });
    });

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (!input.files.length) { status(form, "Take a photo or choose a file first.", true); return; }
      button.disabled = true;
      button.textContent = "Preparing...";
      Promise.all(Array.prototype.map.call(input.files, function (f, i) { return shrink(f, crops[i] || null); }))
        .then(function (files) {
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
          else if (xhr.status === 403) {
            button.disabled = false;
            button.textContent = label;
            status(form, "Not uploaded - your user is not allowed to upload here, or the page has expired. Reload the " +
                         "page; if it happens again, ask the CFA Manager to check your role.", true);
          }
          else if (xhr.status === 413) { failed("the file is too large"); }
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

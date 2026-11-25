// A pointer and a caption drawn over a page while it is filmed (record.js,
// record-claude.js). They sit in the page's top layer (a popover), so a modal
// dialog does not cover them. For the film only: no page of Truejoin has them.
"use strict";

function drawOverlay() {
  if (window.__demo) return;
  const host = document.createElement("div");
  host.setAttribute("popover", "manual");
  Object.assign(host.style, { position: "fixed", inset: "0", width: "100vw", height: "100vh", margin: "0", padding: "0", border: "0", background: "transparent", pointerEvents: "none", overflow: "visible" });
  const pointer = document.createElement("div");
  Object.assign(pointer.style, { position: "absolute", left: "0", top: "0", width: "22px", height: "22px", marginLeft: "-11px", marginTop: "-11px", borderRadius: "50%", background: "rgba(15, 98, 254, 0.35)", border: "2px solid #0f62fe", boxShadow: "0 0 0 2px #fff", transition: "transform 120ms ease", opacity: "0" });
  const caption = document.createElement("div");
  Object.assign(caption.style, { position: "absolute", left: "50%", bottom: "28px", transform: "translateX(-50%)", width: "max-content", maxWidth: "1220px", padding: "10px 20px", background: "rgba(22, 22, 22, 0.92)", color: "#fff", font: '500 19px/28px "IBM Plex Sans", system-ui, sans-serif', textAlign: "center", borderRadius: "4px", opacity: "0", transition: "opacity 200ms ease" });
  host.append(caption, pointer);
  let at = [640, 360];
  const place = (scale) => {
    pointer.style.opacity = "1"; // shown from its first move on
    pointer.style.transform = `translate(${at[0]}px, ${at[1]}px) scale(${scale})`;
  };
  document.addEventListener(
    "mousemove",
    (e) => {
      if (!e.clientX && !e.clientY) return; // where a new page puts the mouse, before anything moved it
      at = [e.clientX, e.clientY];
      place(1);
    },
    true,
  );
  document.addEventListener("mousedown", () => place(0.6), true);
  document.addEventListener("mouseup", () => place(1), true);
  const mount = () => {
    document.body.append(host);
    host.showPopover();
  };
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
  window.__demo = {
    say(text) {
      caption.textContent = text;
      caption.style.opacity = text ? "1" : "0";
    },
    // A dialog opened after the overlay sits above it: put the overlay back on top.
    raise() {
      host.hidePopover();
      host.showPopover();
    },
  };
}

module.exports = { drawOverlay };

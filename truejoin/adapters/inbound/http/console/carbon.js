"use strict";
// Carbon web components for what el() builds: a <button> becomes <cds-button>
// (its class gives the kind), a badge <span> a <cds-tag>; ask() is a Carbon
// modal in place of window.confirm. Loaded before console.js, which uses them.

// The console's "secondary" is a lesser action beside a primary one: Carbon's tertiary
// (an outline). Carbon's own secondary, a filled dark button, is kept for dialogs.
const CARBON_KINDS = { primary: "primary", secondary: "tertiary", tertiary: "tertiary", ghost: "ghost", danger: "danger" };
// Green says it works, red that it does not; blue informs, purple asks for care.
const CARBON_TAGS = {
  "status-approved": "green",
  "status-modified": "purple",
  "status-pending": "gray",
  "status-draft": "gray",
  "tag-blue": "blue",
  "tag-purple": "purple",
  "tag-red": "red",
};

// Native buttons kept as they are: side-list items and inline links.
function carbonable(tag, attrs) {
  const cls = String(attrs.class || "");
  if (tag === "button") return !/\b(item|link)\b/.test(cls);
  return tag === "span" && /\bbadge\b/.test(cls);
}

function carbonEl(tag, attrs, children) {
  const cls = String(attrs.class || "").split(/\s+/).filter(Boolean);
  if (tag === "span") {
    const e = document.createElement("cds-tag");
    e.setAttribute("size", "sm");
    e.setAttribute("type", cls.map((c) => CARBON_TAGS[c]).find(Boolean) || "gray");
    return [e, attrs, children, ["class"]];
  }
  const e = document.createElement("cds-button");
  e.setAttribute("kind", cls.map((c) => CARBON_KINDS[c]).find(Boolean) || "primary");
  e.setAttribute("size", cls.includes("small") ? "sm" : "md");
  if (attrs.type === "submit") {
    // A custom element does not submit its form: ask the form to, as a <button> would.
    e.addEventListener("click", () => e.closest("form")?.requestSubmit());
  }
  return [e, attrs, children, ["class", "type"]];
}

// A Carbon modal for a yes/no; resolves true only on the primary button.
function ask(message, { title = tr("Are you sure?"), yes = tr("Continue"), danger = true } = {}) {
  return new Promise((resolve) => {
    const modal = document.createElement("cds-modal");
    modal.setAttribute("size", "sm");
    let answered = false;
    const finish = (value) => {
      if (answered) return;
      answered = true;
      modal.open = false;
      resolve(value);
      setTimeout(() => modal.remove(), 300);
    };
    const header = document.createElement("cds-modal-header");
    header.append(document.createElement("cds-modal-close-button"));
    const heading = document.createElement("cds-modal-heading");
    heading.textContent = title;
    header.append(heading);
    const body = document.createElement("cds-modal-body");
    const text = document.createElement("p");
    text.textContent = message;
    body.append(text);
    const footer = document.createElement("cds-modal-footer");
    const no = document.createElement("cds-modal-footer-button");
    no.setAttribute("kind", "secondary");
    no.textContent = tr("Cancel");
    no.addEventListener("click", () => finish(false));
    const go = document.createElement("cds-modal-footer-button");
    go.setAttribute("kind", danger ? "danger" : "primary");
    go.textContent = yes;
    go.addEventListener("click", () => finish(true));
    footer.append(no, go);
    modal.append(header, body, footer);
    modal.addEventListener("cds-modal-closed", () => finish(false));
    document.body.append(modal);
    requestAnimationFrame(() => (modal.open = true));
  });
}

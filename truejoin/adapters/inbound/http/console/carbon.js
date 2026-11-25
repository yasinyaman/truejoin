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

// A Carbon modal with a form; resolves with the values by field name, or null when it
// is cancelled. A field is {name, label, type = "text", value = "", autocomplete,
// required = true, minlength} for an input, or {name, label, checks: [values], value: [checked]}
// for a group of checkboxes (resolved as the array of the checked ones). ``note`` says why
// the last try failed, in the server's words (not translated); ``help`` is our own text.
function askForm({ title, yes, fields, note = "", help = "" }) {
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
    const form = document.createElement("form");
    form.className = "login-fields";
    const inputs = [];
    fields.forEach((f, n) => {
      const wrap = document.createElement("div");
      if (f.checks) {
        const set = document.createElement("fieldset");
        set.className = "modes wrap";
        const legend = document.createElement("legend");
        legend.textContent = f.label;
        set.append(legend);
        const boxes = f.checks.map((value) => {
          const label = document.createElement("label");
          const box = document.createElement("input");
          Object.assign(box, { type: "checkbox", value, checked: (f.value || []).includes(value) });
          label.append(box, ` ${value}`);
          set.append(label);
          return box;
        });
        wrap.append(set);
        inputs.push({ name: f.name, read: () => boxes.filter((b) => b.checked).map((b) => b.value) });
      } else {
        const id = `form-${f.name}`;
        const text = document.createElement("label");
        text.htmlFor = id;
        text.textContent = f.label;
        const input = document.createElement("input");
        Object.assign(input, {
          id,
          type: f.type || "text",
          value: f.value || "",
          required: f.required !== false,
          autocomplete: f.autocomplete || "off",
          autocapitalize: "off",
          spellcheck: false,
        });
        if (f.minlength) input.minLength = f.minlength;
        if (n === 0) input.setAttribute("data-modal-primary-focus", "");
        wrap.append(text, input);
        inputs.push({ name: f.name, input, read: () => (f.type === "password" ? input.value : input.value.trim()) });
      }
      form.append(wrap);
    });
    if (help) {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = help;
      form.append(p);
    }
    if (note) {
      const problem = document.createElement("p");
      problem.className = "error";
      problem.setAttribute("role", "alert");
      problem.textContent = note; // the server's words, not ours: not translated
      form.append(problem);
    }
    body.append(form);
    const footer = document.createElement("cds-modal-footer");
    const no = document.createElement("cds-modal-footer-button");
    no.setAttribute("kind", "secondary");
    no.textContent = tr("Cancel");
    no.addEventListener("click", () => finish(null));
    const go = document.createElement("cds-modal-footer-button");
    go.setAttribute("kind", "primary");
    go.textContent = yes;
    const submit = () => {
      if (!form.reportValidity()) return;
      finish(Object.fromEntries(inputs.map((i) => [i.name, i.read()])));
    };
    go.addEventListener("click", submit);
    // A form with several fields and no submit button is not sent by Enter: say so here.
    form.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.target instanceof HTMLButtonElement) return;
      e.preventDefault();
      submit();
    });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      submit();
    });
    footer.append(no, go);
    modal.append(header, body, footer);
    modal.addEventListener("cds-modal-closed", () => finish(null));
    document.body.append(modal);
    // Carbon moves focus to the element marked data-modal-primary-focus when the dialog
    // opens; a plain focus() before it is open does nothing, and the first keys would be lost.
    const first = inputs.find((i) => i.input);
    requestAnimationFrame(() => {
      modal.open = true;
      setTimeout(() => first?.input.focus(), 150);
    });
  });
}

// The directory or local-user sign-in: a username and password, or null when cancelled.
function askLogin(note = "") {
  return askForm({
    title: tr("Sign in"),
    yes: tr("Sign in"),
    note,
    fields: [
      { name: "username", label: tr("Username"), autocomplete: "username" },
      { name: "password", label: tr("Password"), type: "password", autocomplete: "current-password" },
    ],
  });
}

// A panel from the right, for a form too long for a dialog: the list it adds to stays
// in sight behind it. A native <dialog> shown as a modal, so the browser keeps the
// focus inside, closes it on Escape and gives the focus back to what opened it. It
// stays in the page while closed: what was typed is there when it is opened again.
function sidePanel(body = null) {
  const dialog = document.createElement("dialog");
  dialog.className = "side-panel";
  const heading = document.createElement("h2");
  const close = document.createElement("button");
  close.type = "button";
  close.className = "link";
  close.textContent = "×";
  close.setAttribute("aria-label", tr("Close"));
  close.addEventListener("click", () => dialog.close());
  const head = document.createElement("header");
  head.append(heading, close);
  const status = document.createElement("p");
  status.setAttribute("role", "status");
  const content = document.createElement("div");
  if (body) content.append(body);
  dialog.append(head, status, content);
  document.body.append(dialog);
  const panel = {
    get isOpen() {
      return dialog.open;
    },
    // Show it under `title`, with `next` as its content when the content changes.
    open(title, next = null) {
      heading.textContent = title;
      dialog.setAttribute("aria-label", title);
      if (next) content.replaceChildren(next);
      panel.say("");
      if (!dialog.open) dialog.showModal();
    },
    close() {
      if (dialog.open) dialog.close();
    },
    // What the form answered, said inside the panel: the page behind is out of reach.
    say(text, error = false) {
      status.className = error ? "error" : "muted";
      status.textContent = text;
    },
  };
  return panel;
}

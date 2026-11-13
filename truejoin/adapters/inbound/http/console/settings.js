"use strict";
// Settings: everything the configuration file does not fix, changed here and
// served at once (the server rebuilds itself with them before answering).
// From /settings, which needs the admin permission. Uses el, api, $, showView
// from console.js. What the file writes, and the bootstrap settings, show locked.

let settingsRows = [];
let settingsSection = "";

function setSay(text, error = false) {
  $("settings-status").className = error ? "error" : "muted";
  $("settings-status").textContent = text;
}

function setBadges(row) {
  const badges = [];
  if (row.locked === "bootstrap") badges.push(el("span", { class: "badge", title: "Read from the configuration file only" }, "bootstrap"));
  else if (row.locked === "file") badges.push(el("span", { class: "badge", title: "Written in the configuration file, which locks it" }, "file"));
  else if (row.source === "console") badges.push(el("span", { class: "badge status-approved" }, "console"));
  if (row.dangerous) badges.push(el("span", { class: "badge status-modified", title: "Can expose data: confirm by its name" }, "sensitive"));
  if (row.secret) badges.push(el("span", { class: "badge" }, "secret"));
  return badges;
}

function asText(value) {
  if (value === null || value === undefined) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}

// One control per kind; `read(control)` gives the value to send, or undefined when unchanged.
function setControl(row) {
  const disabled = Boolean(row.locked);
  const id = `set-${row.path.replaceAll(".", "-")}`;
  let control;
  if (row.secret && row.locked) {
    // A disabled password box shows nothing at all: say what there is instead.
    control = el("span", { id, class: "muted" }, row.has_value ? "set in the file" : "not set");
  } else if (row.secret) {
    control = el("input", { class: "field", id, type: "password", autocomplete: "new-password", disabled, placeholder: row.has_value ? "set: leave blank to keep" : "" });
  } else if (row.kind === "bool") {
    control = el("input", { id, type: "checkbox", disabled, checked: Boolean(row.value) });
  } else if (row.kind === "choice") {
    control = el("select", { class: "field", id, disabled }, ...row.options.map((o) => el("option", { value: o, selected: o === row.value }, String(o))));
  } else if (row.kind === "number") {
    control = el("input", { class: "field", id, type: "number", step: "any", disabled, value: row.value ?? "", placeholder: row.value === null ? "none" : "" });
  } else if (row.kind === "text") {
    control = el("input", { class: "field", id, disabled, value: row.value ?? "" });
  } else {
    const lines = row.kind === "list" && Array.isArray(row.value) && row.value.every((v) => typeof v === "string");
    control = el(
      "textarea",
      { class: "field", id, rows: 2, spellcheck: "false", disabled, placeholder: row.kind === "list" ? "one per line" : "JSON", "data-lines": lines || (row.kind === "list" && row.value === null) ? "1" : "" },
      lines ? row.value.join("\n") : asText(row.value),
    );
  }
  control.dataset.path = row.path;
  return control;
}

function setRead(row, control) {
  if (row.secret) return control.value ? control.value : undefined;
  let value;
  if (row.kind === "bool") value = control.checked;
  else if (row.kind === "choice") value = control.value;
  else if (row.kind === "number") value = control.value === "" ? null : Number(control.value);
  else if (row.kind === "text") value = control.value;
  else if (control.dataset.lines) {
    const items = control.value.split("\n").map((s) => s.trim()).filter(Boolean);
    value = items.length ? items : row.value === null ? null : [];
  } else {
    const text = control.value.trim();
    value = text ? JSON.parse(text) : null; // a SyntaxError is shown by the caller
  }
  return JSON.stringify(value) === JSON.stringify(row.value) ? undefined : value;
}

function setRow(row, controls) {
  const control = setControl(row);
  controls.push([row, control]);
  const reset = row.source === "console" && !row.locked
    ? el("button", { type: "button", class: "link", onclick: () => saveSettings({}, [row.path]) }, "reset")
    : null;
  const shown = row.secret ? "" : asText(row.default);
  return el(
    "div",
    { class: "setting" },
    el(
      "div",
      {},
      el("label", { for: control.id }, row.label),
      el("div", { class: "muted" }, el("code", {}, row.path)),
      el("div", { class: "badges" }, ...setBadges(row)),
    ),
    el(
      "div",
      {},
      control,
      el("div", { class: "muted" }, row.source === "default" || row.locked ? "" : `default: ${shown || "none"}`, row.changed_by ? ` · changed by ${row.changed_by}` : "", reset ? " · " : "", reset),
    ),
  );
}

function setNav(data) {
  // The file-only ones last: they are there to be read, not changed.
  const sections = [...new Set(settingsRows.map((r) => r.section))].sort((a, b) => (a.startsWith("Startup") ? 1 : 0) - (b.startsWith("Startup") ? 1 : 0));
  if (!sections.includes(settingsSection)) settingsSection = sections[0] || "";
  $("settings-nav").replaceChildren(
    el("h3", {}, "Sections"),
    ...sections.map((s) => {
      const changed = settingsRows.filter((r) => r.section === s && r.source === "console").length;
      return el(
        "button",
        { type: "button", class: s === settingsSection ? "item current" : "item", onclick: () => { settingsSection = s; setRender(data); } },
        s,
        changed ? el("span", { class: "badge status-approved" }, String(changed)) : null,
      );
    }),
    el("p", { class: "muted" }, "Settings written in the configuration file are locked; the rest are served from here, at once."),
  );
}

function setRender(data) {
  setNav(data);
  const rows = settingsRows.filter((r) => r.section === settingsSection);
  const controls = [];
  const may = data.may_change;
  const save = el("button", { type: "submit", disabled: !may }, "Save and apply");
  const form = el(
    "form",
    { class: "settings-form", "aria-label": `${settingsSection} settings` },
    el("h2", {}, settingsSection),
    ...rows.map((r) => setRow(r, controls)),
    el("div", { class: "bar" }, save, el("span", { class: "muted" }, "The server rebuilds itself with the new settings before this answers.")),
  );
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const changes = {};
    try {
      for (const [row, control] of controls) {
        if (row.locked) continue;
        const value = setRead(row, control);
        if (value !== undefined) changes[row.path] = value;
      }
    } catch (err) {
      return setSay(`Not JSON: ${err.message}`, true);
    }
    if (!Object.keys(changes).length) return setSay("Nothing changed.");
    saveSettings(changes, []);
  });
  $("settings-body").replaceChildren(form, el("div", { id: "settings-confirm" }));
}

// Sensitive settings are confirmed by typing their path back.
function setConfirm(paths) {
  return new Promise((resolve) => {
    const inputs = paths.map((p) => el("input", { class: "field", "aria-label": `Type ${p} to confirm`, placeholder: p, autocomplete: "off" }));
    const go = el("button", { type: "button" }, "Apply");
    const box = el(
      "div",
      { class: "term", role: "alertdialog", "aria-label": "Confirm sensitive settings" },
      el("p", {}, el("strong", {}, "These settings can expose data or send it elsewhere."), " Type each one's name to apply it."),
      ...paths.map((p, i) => el("label", {}, el("code", {}, p), inputs[i])),
      el("div", { class: "bar" }, go, el("button", { type: "button", class: "secondary", onclick: () => { box.remove(); resolve(null); } }, "Cancel")),
    );
    go.addEventListener("click", () => {
      if (inputs.every((input, i) => input.value.trim() === paths[i])) {
        box.remove();
        resolve(paths);
      } else setSay("A name does not match: type each path exactly.", true);
    });
    $("settings-confirm").replaceChildren(box);
    box.scrollIntoView({ block: "nearest" });
    inputs[0]?.focus();
  });
}

async function saveSettings(changes, reset) {
  const byPath = Object.fromEntries(settingsRows.map((r) => [r.path, r]));
  const sensitive = [...Object.keys(changes), ...reset].filter((p) => byPath[p]?.dangerous);
  let confirm = [];
  if (sensitive.length) {
    confirm = await setConfirm(sensitive);
    if (!confirm) return setSay("Not applied.");
  }
  setSay("Applying: the server is rebuilding itself with the new settings…");
  try {
    const data = await api("PUT", `${API_PREFIX}/settings`, { changes, reset, confirm });
    settingsRows = data.settings;
    setRender({ may_change: true });
    setSay(`Applied and serving: ${data.changed.join(", ")}.`);
    loadSources();
  } catch (e) {
    setSay(e.message, true);
  }
}

async function loadSettings() {
  setSay("Loading…");
  try {
    const data = await api("GET", `${API_PREFIX}/settings`);
    if (!data.available) {
      $("settings-nav").replaceChildren();
      $("settings-body").replaceChildren(el("p", { class: "muted empty" }, data.detail));
      return setSay("");
    }
    settingsRows = data.settings;
    setRender(data);
    setSay(data.may_change ? "" : "Authentication is off: turn it on to change settings here.");
  } catch (e) {
    $("settings-body").replaceChildren();
    setSay(e.message.startsWith("403") ? "This key cannot change settings: that needs the admin permission." : e.message, true);
  }
}

$("tab-settings").addEventListener("click", () => {
  showView("settings");
  loadSettings();
});
$("auth").addEventListener("submit", () => {
  if (!$("view-settings").hidden) loadSettings();
});

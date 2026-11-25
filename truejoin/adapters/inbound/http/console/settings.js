"use strict";
// Settings: everything the configuration file does not fix, changed here and
// served at once (the server rebuilds itself with them before answering).
// From /settings, which needs the admin permission. Uses el, api, $, page, go
// and address from console.js. What the file writes, and the bootstrap settings, show locked.
//
// An edit is kept until it is saved or discarded: changing section does not
// lose it, the bar at the bottom counts what is unsaved, and one save applies
// every section's edits together.

let settingsRows = [];
let settingsSection = "";
let settingsMay = false;
// path -> what its control holds (text, or true/false), for the edited ones only.
const settingsEdits = new Map();

function setSay(text, error = false) {
  $("settings-status").className = error ? "error" : "muted";
  $("settings-status").textContent = text;
}

function setBadges(row) {
  const badges = [];
  if (row.locked === "bootstrap") badges.push(el("span", { class: "badge", title: tr("Read from the configuration file only") }, tr("file only")));
  else if (row.locked === "file") badges.push(el("span", { class: "badge", title: tr("Written in the configuration file, which locks it") }, tr("locked by the file")));
  else if (row.source === "console") badges.push(el("span", { class: "badge tag-blue", title: tr("Changed here") }, tr("Console")));
  if (row.dangerous) badges.push(el("span", { class: "badge tag-purple", title: tr("Can expose data: confirmed by its name") }, tr("Sensitive")));
  if (row.secret) badges.push(el("span", { class: "badge" }, tr("Secret")));
  return badges;
}

function asText(value) {
  if (value === null || value === undefined) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}

// A list of plain words is edited one per line; anything else written as JSON.
function asLines(row) {
  return row.kind === "list" && (row.value === null || (Array.isArray(row.value) && row.value.every((v) => typeof v === "string")));
}

// What the control of an unedited setting holds.
function rawOf(row) {
  if (row.secret) return "";
  if (row.kind === "bool") return Boolean(row.value);
  if (asLines(row)) return (row.value || []).join("\n");
  if (row.kind === "list" || row.kind === "json") return asText(row.value);
  return row.value === null || row.value === undefined ? "" : String(row.value);
}

// The value to send for what a control holds; throws on text that is not JSON.
function parsed(row, raw) {
  if (row.secret || row.kind === "choice" || row.kind === "text") return raw;
  if (row.kind === "bool") return raw;
  if (row.kind === "number") return raw === "" ? null : Number(raw);
  if (asLines(row)) {
    const items = raw.split("\n").map((s) => s.trim()).filter(Boolean);
    return items.length || row.value !== null ? items : null;
  }
  return raw.trim() ? JSON.parse(raw) : null;
}

function setControl(row) {
  const disabled = Boolean(row.locked) || !settingsMay;
  const id = `set-${row.path.replaceAll(".", "-")}`;
  const raw = settingsEdits.has(row.path) ? settingsEdits.get(row.path) : rawOf(row);
  let control;
  if (row.secret && row.locked) {
    // A disabled password box shows nothing at all: say what there is instead.
    return el("span", { id, class: "muted" }, row.has_value ? tr("set in the file") : tr("not set"));
  }
  if (row.secret) {
    control = el("input", { class: "field", id, type: "password", autocomplete: "new-password", disabled, placeholder: row.has_value ? tr("set: leave blank to keep") : "", value: raw });
  } else if (row.kind === "bool") {
    control = el("input", { id, type: "checkbox", disabled, checked: raw });
  } else if (row.kind === "choice") {
    control = el("select", { class: "field", id, disabled }, ...row.options.map((o) => el("option", { value: o, selected: String(o) === raw }, String(o))));
  } else if (row.kind === "number") {
    control = el("input", { class: "field", id, type: "number", step: "any", disabled, value: raw, placeholder: row.value === null ? tr("None") : "" });
  } else if (row.kind === "text") {
    control = el("input", { class: "field", id, disabled, value: raw });
  } else {
    control = el("textarea", { class: "field", id, rows: asLines(row) ? 3 : 2, spellcheck: "false", disabled, placeholder: asLines(row) ? tr("one per line") : row.example || "JSON" }, raw);
  }
  const edited = () => {
    const now = control.type === "checkbox" ? control.checked : control.value;
    if (now === rawOf(row)) settingsEdits.delete(row.path);
    else settingsEdits.set(row.path, now);
    control.closest(".setting")?.classList.toggle("changed", settingsEdits.has(row.path));
    setBar();
    setNav();
  };
  control.addEventListener("input", edited);
  control.addEventListener("change", edited);
  return control;
}

function setRow(row) {
  const control = setControl(row);
  const reset =
    row.source === "console" && !row.locked && settingsMay
      ? el("button", { type: "button", class: "link", title: tr("Back to the file's or the default value"), onclick: () => saveSettings({}, [row.path]) }, tr("Reset"))
      : null;
  const facts = [];
  if (row.source === "console" && !row.secret) facts.push(tr`default: ${asText(row.default) || tr("None")}`);
  if (row.changed_by) facts.push(tr`changed by ${row.changed_by}`);
  if (row.example && !row.locked) facts.push(tr`for example ${row.example}`);
  return el(
    "div",
    { class: settingsEdits.has(row.path) ? "setting changed" : "setting" },
    el(
      "div",
      {},
      el("label", { for: control.id }, tr(row.label)),
      row.help ? el("p", { class: "help" }, tr(row.help)) : null,
      el("div", { class: "muted" }, el("code", {}, row.path)),
      el("div", { class: "badges" }, ...setBadges(row)),
    ),
    el("div", {}, control, el("div", { class: "muted" }, facts.join(" · "), reset ? " " : "", reset)),
  );
}

// A section's place in the address: "Analytics engine" -> analytics-engine. Made
// from the server's own title, which is never looked up as text.
function sectionSlug(section) {
  return section.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function sectionsOf() {
  // The file-only ones last: they are there to be read, not changed.
  return [...new Set(settingsRows.map((r) => r.section))].sort((a, b) => (a.startsWith("Startup") ? 1 : 0) - (b.startsWith("Startup") ? 1 : 0));
}

function unsavedIn(section) {
  return settingsRows.filter((r) => r.section === section && settingsEdits.has(r.path)).length;
}

function setNav() {
  const sections = sectionsOf();
  if (!sections.includes(settingsSection)) settingsSection = sections[0] || "";
  $("settings-nav").replaceChildren(
    el("h3", {}, tr("Sections")),
    ...sections.map((s) => {
      const unsaved = unsavedIn(s);
      return el(
        "a",
        { class: s === settingsSection ? "item current" : "item", href: hashOf("settings", sectionSlug(s)), "aria-current": s === settingsSection ? "page" : null },
        tr(s),
        unsaved ? el("span", { class: "badge tag-blue", title: tr("Unsaved changes") }, tr`${unsaved} unsaved`) : null,
      );
    }),
    el("p", { class: "muted" }, tr("Settings written in the configuration file are locked; the rest are served from here, at once.")),
  );
}

// The bar at the bottom: what is unsaved, across every section, and what to do with it.
function setBar() {
  const n = settingsEdits.size;
  const where = sectionsOf().filter((s) => unsavedIn(s));
  // replaceChildren, unlike el(), would print a null as the text "null".
  const parts = [
    el("button", { type: "button", disabled: !n || !settingsMay, onclick: saveEdits }, n ? tr`Save and apply ${n} change(s)` : tr("Save and apply")),
    n ? el("button", { type: "button", class: "ghost", onclick: discardEdits }, tr("Discard")) : null,
    el("span", { class: "muted" }, n ? tr`Unsaved in: ${where.map((s) => tr(s)).join(", ")}.` : tr("Nothing to save. The server rebuilds itself with the new settings when you do.")),
  ];
  $("settings-bar").replaceChildren(...parts.filter(Boolean));
}

function setRender() {
  setNav();
  const rows = settingsRows.filter((r) => r.section === settingsSection);
  const parts = [el("h2", {}, tr(settingsSection))];
  if (settingsSection.startsWith("Startup")) {
    parts.push(el("p", { class: "muted" }, tr("What the server reads before anything else. Change these in the configuration file and restart.")));
  }
  let group = null;
  for (const row of rows) {
    if (row.group !== group) {
      group = row.group;
      if (group) parts.push(el("h3", { class: "group" }, tr(group)), row.group_help ? el("p", { class: "muted" }, tr(row.group_help)) : null);
    }
    parts.push(setRow(row));
  }
  $("settings-body").replaceChildren(
    el("div", { class: "settings-form", role: "group", "aria-label": tr(settingsSection) }, ...parts),
    el("div", { id: "settings-confirm" }),
    el("div", { id: "settings-bar", class: "save-bar" }),
  );
  setBar();
}

// Sensitive settings are confirmed by typing their path back.
function setConfirm(paths) {
  return new Promise((resolve) => {
    const inputs = paths.map((p) => el("input", { class: "field", "aria-label": tr`Type ${p} to confirm`, placeholder: p, autocomplete: "off" }));
    const go = el("button", { type: "button" }, tr("Apply"));
    const box = el(
      "div",
      { class: "term", role: "alertdialog", "aria-label": tr("Confirm sensitive settings") },
      el("p", {}, el("strong", {}, tr("These settings can expose data or send it elsewhere.")), " ", tr("Type each one's name to apply it.")),
      ...paths.map((p, i) => el("label", {}, el("code", {}, p), inputs[i])),
      el(
        "div",
        { class: "bar" },
        go,
        el(
          "button",
          {
            type: "button",
            class: "ghost",
            onclick: () => {
              box.remove();
              resolve(null);
            },
          },
          tr("Cancel"),
        ),
      ),
    );
    go.addEventListener("click", () => {
      if (inputs.every((input, i) => input.value.trim() === paths[i])) {
        box.remove();
        resolve(paths);
      } else setSay(tr("A name does not match: type each path exactly."), true);
    });
    $("settings-confirm").replaceChildren(box);
    box.scrollIntoView({ block: "center" });
    inputs[0]?.focus();
  });
}

function saveEdits() {
  const byPath = Object.fromEntries(settingsRows.map((r) => [r.path, r]));
  const changes = {};
  for (const [path, raw] of settingsEdits) {
    try {
      changes[path] = parsed(byPath[path], raw);
    } catch (e) {
      settingsSection = byPath[path].section;
      setRender();
      return setSay(tr`${path} is not JSON: ${e.message}`, true);
    }
  }
  return saveSettings(changes, []);
}

function discardEdits() {
  settingsEdits.clear();
  setRender();
  setSay(tr("Discarded: nothing was changed."));
}

async function saveSettings(changes, reset) {
  const byPath = Object.fromEntries(settingsRows.map((r) => [r.path, r]));
  const touched = [...Object.keys(changes), ...reset];
  if (!touched.length) return setSay(tr("Nothing changed."));
  const sensitive = touched.filter((p) => byPath[p]?.dangerous);
  let confirm = [];
  if (sensitive.length) {
    confirm = await setConfirm(sensitive);
    if (!confirm) return setSay(tr("Not applied."));
  }
  setSay(tr("Applying: the server is rebuilding itself with the new settings…"));
  try {
    const data = await api("PUT", `${API_PREFIX}/settings`, { changes, reset, confirm });
    settingsRows = data.settings;
    for (const path of touched) settingsEdits.delete(path);
    setRender();
    setSay(tr`Applied and serving: ${data.changed.join(", ")}.`);
    loadSources();
  } catch (e) {
    setSay(e.message, true);
  }
}

async function loadSettings() {
  setSay(tr("Loading…"));
  try {
    const data = await api("GET", `${API_PREFIX}/settings`);
    if (!data.available) {
      $("settings-nav").replaceChildren();
      $("settings-body").replaceChildren(el("p", { class: "muted empty" }, data.detail));
      return setSay("");
    }
    settingsRows = data.settings;
    settingsMay = Boolean(data.may_change);
    setRender();
    setSay(settingsMay ? "" : tr("Authentication is off: turn it on to change settings here."));
  } catch (e) {
    $("settings-body").replaceChildren();
    setSay(e.status === 403 ? tr("This key cannot change settings: that needs the admin permission.") : e.message, true);
  }
}

// Opens Settings on one setting: its section, its control in focus. The section is
// found once the settings are read, so the address names the setting alone.
function showSetting(path) {
  go("settings", "-", path);
}

// #settings, #settings/<section>, #settings/<section or ->/<setting.path>
page("settings", {
  async open([slug, path]) {
    // Coming back keeps what was being edited: the rows on screen are the ones being edited.
    if (!settingsEdits.size || !settingsRows.length) await loadSettings();
    const row = path && settingsRows.find((r) => r.path === path);
    const section = row ? row.section : sectionsOf().find((s) => sectionSlug(s) === slug);
    if (section) settingsSection = section;
    if (settingsRows.length) {
      setRender();
      address("settings", [sectionSlug(settingsSection), row ? path : null], true);
    }
    if (!row) return;
    const control = $(`set-${path.replaceAll(".", "-")}`);
    control?.scrollIntoView({ block: "center" });
    control?.focus({ preventScroll: true });
  },
  reload: () => loadSettings(),
});
window.addEventListener("beforeunload", (e) => {
  if (settingsEdits.size) e.preventDefault();
});

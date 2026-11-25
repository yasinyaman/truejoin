"use strict";
// API keys: hand one out, see who holds what, revoke. From /keys, which needs
// the admin permission. Uses el, api and $ from console.js. A new key's secret
// is shown once, in this page, and kept by the server only as a digest.

const KEY_PERMISSIONS = ["read", "query", "create", "update", "delete", "audit", "admin", "all"];

function keyForm() {
  const perms = el(
    "fieldset",
    { class: "modes wrap" },
    el("legend", { class: "sr-only" }, tr("Permissions")),
    ...KEY_PERMISSIONS.map((p) => el("label", {}, el("input", { type: "checkbox", name: "permissions", value: p, checked: p === "read" }), ` ${p}`)),
  );
  const form = el(
    "form",
    { class: "filters", "aria-label": tr("Hand out a key") },
    el("label", {}, tr("Name (who or what holds it)"), el("input", { class: "field", name: "name", required: true, maxlength: 64, placeholder: "etl-nightly" })),
    el("label", {}, tr("Permissions"), perms),
    el("label", {}, tr("Tenant"), el("input", { class: "field", name: "tenant", placeholder: tr("None") })),
    el("label", {}, tr("Expires after (days)"), el("input", { class: "field", name: "expires_in_days", type: "number", min: 1, max: 3650, placeholder: tr("Never") })),
    rulesEditor(),
    el("div", { class: "bar" }, el("button", { type: "submit" }, tr("Hand out")), el("button", { type: "button", class: "secondary", onclick: () => previewKey(form) }, tr("Check the rules"))),
    el("div", { id: "keys-preview" }),
    el("p", { class: "muted" }, tr("You can give only what your own key holds, and a new key carries your roles. A key cannot be edited: revoke it and hand out another.")),
  );
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    createKey(form);
  });
  return form;
}

// -- row rules: a form, the same rules as JSON, and a check of what they leave --

const RULE_OPERATORS = ["eq", "ne", "gt", "gte", "lt", "lte", "like", "in", "is_null"];

// One rule per row: table, column, operator, value. The JSON below is the same
// rules, kept in step both ways, for what the form cannot say (a number, a list).
function rulesEditor() {
  const list = el("div", { class: "rules" });
  const json = el("textarea", { name: "row_filters", rows: 3, spellcheck: "false", placeholder: '{"orders": [{"column": "tenant", "value": "${tenant}"}]}', "aria-label": tr("Row rules as JSON") });
  const sync = () => (json.value = list.childElementCount ? JSON.stringify(rulesOf(list), null, 1) : "");
  const add = (rule = {}) => {
    const row = el(
      "div",
      { class: "rule" },
      el("input", { class: "field", name: "rule-table", placeholder: tr("Table"), "aria-label": tr("Table"), value: rule.table || "", list: "rule-tables" }),
      el("input", { class: "field", name: "rule-column", placeholder: tr("Column"), "aria-label": tr("Column"), value: rule.column || "" }),
      el("select", { class: "field", name: "rule-op", "aria-label": tr("Operator") }, ...RULE_OPERATORS.map((op) => el("option", { value: op, selected: (rule.operator || rule.op || "eq") === op }, op))),
      el("input", { class: "field", name: "rule-value", placeholder: "${tenant}", "aria-label": tr("Value"), value: rule.value === undefined ? "" : Array.isArray(rule.value) ? rule.value.join(", ") : String(rule.value) }),
      el("button", { type: "button", class: "link", "aria-label": tr("Remove this rule"), onclick: () => (row.remove(), sync()) }, "×"),
    );
    row.addEventListener("input", sync);
    row.addEventListener("change", sync);
    list.append(row);
  };
  json.addEventListener("change", () => {
    const text = json.value.trim();
    let parsed = {};
    try {
      parsed = text ? JSON.parse(text) : {};
    } catch (e) {
      return keysSay(tr`The row rules are not JSON: ${e.message}`, true);
    }
    list.replaceChildren();
    for (const [table, rules] of Object.entries(parsed)) for (const rule of rules || []) add({ ...rule, table });
  });
  const tables = el("datalist", { id: "rule-tables" });
  return el(
    "fieldset",
    { class: "rules-box" },
    el("legend", {}, tr("Row rules")),
    el("p", { class: "muted" }, tr("Rows of a table this key may see: every rule on a table must hold. ${tenant} is the key's tenant; ${username}, its name.")),
    list,
    tables,
    el("button", { type: "button", class: "link", onclick: () => (add(), sync()) }, tr("Add a rule")),
    el("details", {}, el("summary", {}, tr("As JSON, as in the configuration")), json),
  );
}

// The rules the form holds, as the server takes them: an "in" value is a list,
// an "is_null" one true or false; anything else stays the text typed.
function rulesOf(list) {
  const rules = {};
  for (const row of list.querySelectorAll(".rule")) {
    const get = (name) => row.querySelector(`[name="${name}"]`).value.trim();
    const table = get("rule-table");
    const column = get("rule-column");
    if (!table && !column) continue;
    const operator = get("rule-op");
    const text = get("rule-value");
    const value = operator === "in" ? text.split(",").map((v) => v.trim()).filter(Boolean) : operator === "is_null" ? text !== "false" : text;
    (rules[table] ||= []).push({ column, operator, value });
  }
  return rules;
}

function keyBody(form) {
  const data = new FormData(form);
  const body = {
    name: String(data.get("name") || "").trim(),
    permissions: data.getAll("permissions").map(String),
  };
  const tenant = String(data.get("tenant") || "").trim();
  if (tenant) body.tenant = tenant;
  const days = String(data.get("expires_in_days") || "").trim();
  if (days) body.expires_in_days = Number(days);
  const rules = String(data.get("row_filters") || "").trim();
  if (rules) body.row_filters = JSON.parse(rules); // throws: said by the caller
  return body;
}

async function createKey(form) {
  let body;
  try {
    body = keyBody(form);
  } catch (e) {
    return keysSay(tr`The row rules are not JSON: ${e.message}`, true);
  }
  try {
    const made = await api("POST", `${API_PREFIX}/keys`, body);
    form.reset();
    form.querySelector(".rules").replaceChildren();
    $("keys-preview").replaceChildren();
    // The panel gives the focus back to its button; the secret then takes it.
    keyPanel.close();
    showSecret(made.key.name, made.secret);
    await loadKeys(false);
  } catch (e) {
    keysSay(e.message, true);
  }
}

// What the rules would leave, table by table, before the key exists.
async function previewKey(form) {
  let body;
  try {
    body = keyBody(form);
  } catch (e) {
    return keysSay(tr`The row rules are not JSON: ${e.message}`, true);
  }
  if (!body.row_filters || !Object.keys(body.row_filters).length) return keysSay(tr("Add a rule first: without one, the key sees every row its permissions allow."), true);
  keysSay(tr("Counting…"));
  try {
    const { problems, tables } = await api("POST", `${API_PREFIX}/keys/preview`, { name: body.name || "preview", tenant: body.tenant, row_filters: body.row_filters });
    keysSay("");
    $("keys-preview").replaceChildren(
      el("h3", {}, tr("What these rules leave")),
      ...problems.map((p) => el("p", { class: "warn" }, p)),
      tables.length
        ? keysTable(
            [tr("Table"), tr("Rows this key would see"), tr("Rows you see")],
            tables.map((t) => el("tr", {}, el("td", {}, t.table), el("td", { class: "num" }, t.error ? "" : t.visible.toLocaleString()), el("td", { class: "num" }, t.error ? el("span", { class: "error" }, t.error) : t.total.toLocaleString()))),
          )
        : el("p", { class: "muted" }, tr("No connected table matches these rules.")),
      el("p", { class: "muted" }, tr("Counted as you, and recorded in the audit trail under your name. No value is shown.")),
    );
  } catch (e) {
    keysSay(e.status === 501 ? tr("Rules can be checked only where the analytics engine runs.") : e.message, true);
  }
}

// The one time the secret exists outside its holder's hands.
function showSecret(name, secret) {
  const value = el("input", { class: "field", readonly: true, value: secret, "aria-label": tr`The key for ${name}` });
  const copy = el("button", { type: "button", class: "secondary" }, tr("Copy"));
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(secret);
      copy.textContent = tr("Copied");
    } catch {
      value.select();
    }
  });
  const box = el(
    "div",
    { class: "term", role: "alert" },
    el("p", {}, el("strong", {}, tr`The key for ${name}`), tr(" — shown this once. Copy it now; it cannot be shown again.")),
    el("div", { class: "bar" }, value, copy, el("button", { type: "button", class: "link", onclick: () => box.remove() }, tr("Done"))),
  );
  $("keys-secret").replaceChildren(box);
  value.select();
}

function keysSay(text, error = false) {
  if (keyPanel.isOpen) return keyPanel.say(text, error); // the form's own answer
  $("keys-status").className = error ? "error" : "muted";
  $("keys-status").textContent = text;
}

function keyState(k) {
  if (k.revoked_at) return el("span", { class: "badge" }, tr("Revoked"));
  if (!k.active) return el("span", { class: "badge tag-red" }, tr("Expired"));
  return el("span", { class: "badge status-approved" }, tr("Active"));
}

function day(iso) {
  return iso ? iso.slice(0, 10) : "";
}

function grantCells(k) {
  const rules = Object.entries(k.row_filters || {});
  return [
    el("td", {}, k.permissions.join(", ")),
    el("td", {}, [k.tenant ? tr`tenant ${k.tenant}` : "", (k.roles || []).join(", ")].filter(Boolean).join(" · ")),
    el("td", {}, rules.length ? rules.map(([table, list]) => `${table}: ${list.map((r) => r.column).join(", ")}`).join("; ") : ""),
  ];
}

function keyRow(k) {
  const revoke = el("button", { type: "button", class: "secondary small" }, tr("Revoke"));
  revoke.addEventListener("click", async () => {
    if (!(await ask(tr`Whoever holds it is refused from the next request on. This cannot be undone.`, { title: tr`Revoke ${k.name}?`, yes: tr("Revoke") }))) return;
    try {
      await api("DELETE", `${API_PREFIX}/keys/${encodeURIComponent(k.id)}`);
      keysSay(tr`${k.name} is revoked.`);
      await loadKeys(false);
    } catch (e) {
      keysSay(e.message, true);
    }
  });
  return el(
    "tr",
    {},
    el("td", {}, k.name, el("div", { class: "muted" }, `…${k.hint}`)),
    el("td", {}, keyState(k)),
    ...grantCells(k),
    el("td", {}, day(k.created_at), el("div", { class: "muted" }, k.created_by || "")),
    el("td", {}, k.revoked_at ? tr`revoked ${day(k.revoked_at)}` : k.expires_at ? day(k.expires_at) : tr("Never")),
    el("td", {}, k.revoked_at || !k.active ? null : revoke),
  );
}

function keysTable(head, rows) {
  return el(
    "div",
    { class: "scroll" },
    el("table", { class: "review" }, el("thead", {}, el("tr", {}, ...head.map((h) => el("th", {}, h)))), el("tbody", {}, ...rows)),
  );
}

// The tables a rule can name, offered as the table field is typed.
async function loadRuleTables() {
  try {
    const { sources } = await api("GET", "/olap/sources");
    $("rule-tables").replaceChildren(...sources.flatMap((s) => s.tables.map((t) => el("option", { value: t.name.slice(t.name.indexOf(".") + 1) }))));
  } catch {} // without the query permission, tables are typed by hand
}

async function loadKeys(clearStatus = true) {
  if (clearStatus) keysSay(tr("Loading…"));
  loadRuleTables();
  try {
    const data = await api("GET", `${API_PREFIX}/keys`);
    $("keys-actions").hidden = !data.available;
    const handed = data.keys.length
      ? keysTable([tr("Name"), tr("State"), tr("Permissions"), tr("Tenant · roles"), tr("Row rules on"), tr("Made"), tr("Expires"), ""], data.keys.map(keyRow))
      : el("p", { class: "muted" }, tr("No key handed out yet."));
    const configured = !data.configured.length
      ? el("p", { class: "muted" }, tr("No key in the configuration."))
      : keysTable(
      [tr("Name"), tr("Permissions"), tr("Tenant · roles"), tr("Row rules")],
      data.configured.map((k) =>
        el(
          "tr",
          {},
          el("td", {}, k.name),
          el("td", {}, k.permissions.join(", ")),
          el("td", {}, [k.tenant ? tr`tenant ${k.tenant}` : "", (k.roles || []).join(", ")].filter(Boolean).join(" · ")),
          el("td", {}, k.row_rules ? tr("Yes") : ""),
        ),
      ),
    );
    $("keys-body").replaceChildren(
      el("h3", {}, tr("Handed out here")),
      data.available ? handed : el("p", { class: "muted" }, data.detail),
      el("h3", {}, tr("From the configuration")),
      el("p", { class: "muted" }, tr("Changed in the configuration file, not here.")),
      configured,
    );
    if (clearStatus) keysSay(data.auth_enabled === false ? tr("Authentication is off: no key is checked until it is turned on.") : "");
  } catch (e) {
    $("keys-actions").hidden = true;
    $("keys-body").replaceChildren();
    keysSay(e.status === 403 ? tr("This key cannot manage keys: that needs the admin permission.") : e.message, true);
  }
}

const keyPanel = sidePanel(keyForm());
$("keys-actions").replaceChildren(el("button", { type: "button", onclick: () => keyPanel.open(tr("Hand out a key")) }, tr("Hand out a key")));
page("keys", { needs: "admin", open: () => loadKeys() });

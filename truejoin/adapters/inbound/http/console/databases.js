"use strict";
// Databases: add a connection, change one, remove one, without a restart. From
// /databases, which needs the admin permission. Uses el, api, $, showView and
// loadSources from console.js. A password goes to the server and never comes
// back: it is kept encrypted there, and a blank one on a change keeps it.

const DEFAULT_PORTS = { postgresql: 5432, postgres: 5432, mysql: 3306, mariadb: 3306, mssql: 1433, sqlserver: 1433, oracle: 1521 };
let dbTypes = ["postgresql", "mysql", "mssql", "oracle"];
let dbEditing = null; // the added database being changed, or null for a new one

function dbSay(text, error = false) {
  $("databases-status").className = error ? "error" : "muted";
  $("databases-status").textContent = text;
}

function dbField(label, name, attrs = {}) {
  return el("label", {}, label, el("input", { class: "field", name, ...attrs }));
}

function dbForm(database = null) {
  dbEditing = database;
  const editing = database !== null;
  const options = database ? { ...(database.options || {}) } : {};
  const pool = options.pool_size ?? "";
  const schema = options.schema ?? "";
  const ssl = Boolean(options.ssl);
  delete options.pool_size;
  delete options.schema;
  delete options.ssl;
  const type = el("select", { class: "field", name: "type", required: true }, ...dbTypes.map((t) => el("option", { value: t, selected: database?.type === t }, t)));
  const port = el("input", { class: "field", name: "port", type: "number", min: 1, max: 65535, value: database?.port ?? DEFAULT_PORTS[dbTypes[0]] ?? "" });
  type.addEventListener("change", () => {
    if (DEFAULT_PORTS[type.value]) port.value = DEFAULT_PORTS[type.value];
  });
  const keep = tr("leave blank to keep");
  const form = el(
    "form",
    { class: "filters", "aria-label": editing ? tr`Change ${database.name}` : tr("Add a database") },
    el("h3", {}, editing ? tr`Change ${database.name}` : tr("Add a database")),
    dbField(tr("Name (the URL segment and SQL schema)"), "name", { required: true, maxlength: 63, placeholder: "sales", value: database?.name ?? "", readonly: editing, pattern: "[A-Za-z_][A-Za-z0-9_]*" }),
    el("label", {}, tr("Type"), type),
    dbField(tr("Host"), "host", { placeholder: "localhost", value: database?.host ?? "" }),
    el("label", {}, tr("Port"), port),
    dbField(tr("Database"), "database", { required: true, value: database?.database ?? "" }),
    dbField(tr("User"), "username", { required: true, autocomplete: "off", value: database?.username ?? "" }),
    dbField(tr("Password"), "password", { type: "password", autocomplete: "new-password", placeholder: editing && database.has_password ? keep : "" }),
    el(
      "details",
      { class: "try", open: Boolean(database?.readonly_username || pool || schema || ssl || Object.keys(options).length) },
      el("summary", {}, tr("More")),
      dbField(tr("Read-only user (raw SQL and OLAP use it)"), "readonly_username", { autocomplete: "off", value: database?.readonly_username ?? "" }),
      dbField(tr("Read-only password"), "readonly_password", { type: "password", autocomplete: "new-password", placeholder: editing && database.has_readonly_password ? keep : "" }),
      dbField(tr("Pool size"), "pool_size", { type: "number", min: 1, max: 200, placeholder: "10", value: pool }),
      dbField(tr("Schema"), "schema", { placeholder: tr("public / dbo / the database"), value: schema }),
      el("label", { class: "check" }, el("input", { type: "checkbox", name: "ssl", checked: ssl }), tr(" SSL (PostgreSQL)")),
      el(
        "label",
        {},
        tr("Other options (JSON)"),
        el("textarea", { name: "options", rows: 2, spellcheck: "false", placeholder: '{"trust_server_certificate": true}' }, Object.keys(options).length ? JSON.stringify(options) : ""),
      ),
    ),
    el(
      "div",
      { class: "bar" },
      el("button", { type: "submit" }, editing ? tr("Save changes") : tr("Add")),
      el("button", { type: "button", class: "secondary", onclick: () => testDatabase(form) }, tr("Test connection")),
      editing ? el("button", { type: "button", class: "link", onclick: () => $("databases-form").replaceChildren(dbForm()) }, tr("Cancel")) : null,
    ),
    el(
      "p",
      { class: "muted" },
      editing
        ? tr("The new settings are tried first; if they fail, the database keeps serving as it was.")
        : tr("It is connected first and kept only if that works. Its tables are served at once: the API, SQL and the agents' tools."),
    ),
  );
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    saveDatabase(form);
  });
  return form;
}

// The request body: on a change, blank passwords are left out so the kept ones stay.
function dbBody(form) {
  const data = new FormData(form);
  const text = (name) => String(data.get(name) || "").trim();
  const body = { name: text("name"), type: text("type"), host: text("host") || "localhost", database: text("database"), username: text("username") };
  if (text("port")) body.port = Number(text("port"));
  for (const secret of ["password", "readonly_password"]) {
    const value = String(data.get(secret) || "");
    if (value || !dbEditing) body[secret] = value;
  }
  body.readonly_username = text("readonly_username");
  let options = {};
  if (text("options")) options = JSON.parse(text("options")); // a SyntaxError is shown by the caller
  if (text("pool_size")) options.pool_size = Number(text("pool_size"));
  if (text("schema")) options.schema = text("schema");
  if (data.get("ssl")) options.ssl = true;
  body.options = options;
  return body;
}

async function withBody(form, action) {
  let body;
  try {
    body = dbBody(form);
  } catch (e) {
    return dbSay(tr`The other options are not JSON: ${e.message}`, true);
  }
  const buttons = [...form.querySelectorAll("button, cds-button")];
  buttons.forEach((b) => (b.disabled = true));
  try {
    await action(body);
  } catch (e) {
    dbSay(e.message, true);
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

function testDatabase(form) {
  dbSay(tr("Connecting…"));
  return withBody(form, async (body) => {
    await api("POST", `${API_PREFIX}/databases/test`, body);
    dbSay(tr`${body.name || tr("It")} answers: the connection works.`);
  });
}

function saveDatabase(form) {
  const editing = dbEditing;
  dbSay(editing ? tr`Changing ${editing.name}…` : tr("Connecting and adding…"));
  return withBody(form, async (body) => {
    if (editing) await api("PUT", `${API_PREFIX}/databases/${encodeURIComponent(editing.name)}`, body);
    else await api("POST", `${API_PREFIX}/databases`, body);
    $("databases-form").replaceChildren(dbForm());
    await loadDatabases(false);
    loadSources();
    dbSay(editing ? tr`${editing.name} is changed and serving.` : tr`${body.name} is added and serving.`);
  });
}

function dbState(d) {
  return d.state === "connected" ? el("span", { class: "badge status-approved" }, tr("connected")) : el("span", { class: "badge tag-red", title: d.error || "" }, tr("not connected"));
}

function dbWhere(d) {
  return `${d.host}:${d.port} / ${d.database}`;
}

function dbRow(d) {
  const change = el("button", { type: "button", class: "secondary small", onclick: () => $("databases-form").replaceChildren(dbForm(d)) }, tr("Change"));
  const remove = el("button", { type: "button", class: "link" }, tr("Remove"));
  remove.addEventListener("click", async () => {
    if (!(await ask(tr`Its tables stop being served at once, by the API, SQL and the agents' tools. Its catalog is kept.`, { title: tr`Remove ${d.name}?`, yes: tr("Remove") }))) return;
    try {
      await api("DELETE", `${API_PREFIX}/databases/${encodeURIComponent(d.name)}`);
      if (dbEditing?.name === d.name) $("databases-form").replaceChildren(dbForm());
      await loadDatabases(false);
      loadSources();
      dbSay(tr`${d.name} is removed.`);
    } catch (e) {
      dbSay(e.message, true);
    }
  });
  return el(
    "tr",
    {},
    el("td", {}, d.name),
    el("td", {}, dbState(d), d.error ? el("div", { class: "muted" }, d.error) : null),
    el("td", {}, d.type),
    el("td", {}, dbWhere(d)),
    el("td", {}, d.username, d.readonly_username ? el("div", { class: "muted" }, tr`read-only: ${d.readonly_username}`) : null),
    el("td", {}, (d.updated_at || d.created_at || "").slice(0, 10), el("div", { class: "muted" }, d.updated_by || d.created_by || "")),
    el("td", { class: "nowrap" }, change, " ", remove),
  );
}

function dbTable(head, rows) {
  return el("div", { class: "scroll" }, el("table", { class: "review" }, el("thead", {}, el("tr", {}, ...head.map((h) => el("th", {}, h)))), el("tbody", {}, ...rows)));
}

async function loadDatabases(clearStatus = true) {
  if (clearStatus) dbSay(tr("Loading…"));
  try {
    const data = await api("GET", `${API_PREFIX}/databases`);
    // The usual ones first; the aliases (postgres, mariadb, sqlserver) are left out.
    const order = ["postgresql", "mysql", "mssql", "oracle", "odbc"];
    if (data.types?.length) dbTypes = order.filter((t) => data.types.includes(t)).concat(data.types.filter((t) => !order.includes(t) && !["postgres", "mariadb", "sqlserver"].includes(t)));
    const form = $("databases-form");
    form.hidden = !data.available || !data.may_change;
    if (!form.hidden && !form.childElementCount) form.replaceChildren(dbForm());
    const added = !data.available
      ? el("p", { class: "muted" }, data.detail)
      : data.databases.length
        ? dbTable([tr("Name"), tr("State"), tr("Type"), tr("Where"), tr("User"), tr("Changed"), ""], data.databases.map(dbRow))
        : el("p", { class: "muted empty" }, tr("No database added here yet. Fill in the form to add one."));
    const configured = data.configured.length
      ? dbTable(
          [tr("Name"), tr("State"), tr("Type"), tr("Where")],
          data.configured.map((d) => el("tr", {}, el("td", {}, d.name), el("td", {}, dbState(d)), el("td", {}, d.type), el("td", {}, dbWhere(d)))),
        )
      : el("p", { class: "muted" }, tr("None."));
    $("databases-body").replaceChildren(
      el("h3", {}, tr("Added here")),
      added,
      el("h3", {}, tr("From the configuration")),
      el("p", { class: "muted" }, tr("Changed in the configuration file, not here.")),
      configured,
    );
    if (clearStatus) dbSay(data.available && !data.may_change ? tr("Authentication is off: turn it on to add databases here.") : "");
  } catch (e) {
    $("databases-form").hidden = true;
    $("databases-body").replaceChildren();
    dbSay(e.status === 403 ? tr("This key cannot manage databases: that needs the admin permission.") : e.message, true);
  }
}

$("tab-databases").addEventListener("click", () => {
  showView("databases");
  loadDatabases();
});
$("auth").addEventListener("submit", () => {
  if (!$("view-databases").hidden) loadDatabases();
});

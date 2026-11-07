"use strict";
// Every value from the server is placed with text nodes, never as HTML.

// Shared with catalog.js, which loads after this file.
const KEY_HEADER = document.querySelector('meta[name="trio-key-header"]').content;
const API_PREFIX = document.querySelector('meta[name="trio-api-prefix"]').content;
const STORE = "trio-console-key";
const $ = (id) => document.getElementById(id);
let mode = "sql";

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  e.append(...children.filter((c) => c !== null && c !== undefined));
  return e;
}

function remember(key) {
  // Tab-scoped, and the page works without it (private windows, blocked storage).
  try {
    if (key) sessionStorage.setItem(STORE, key);
    else sessionStorage.removeItem(STORE);
  } catch {}
}

function recalled() {
  try {
    return sessionStorage.getItem(STORE) || "";
  } catch {
    return "";
  }
}

async function api(method, path, body) {
  const headers = {};
  const key = $("key").value.trim();
  if (key) headers[KEY_HEADER] = key;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try {
    data = await response.json();
  } catch {}
  if (!response.ok) {
    const detail = data.detail ?? data.error;
    const text = typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : "";
    throw new Error(`${response.status}: ${text || response.statusText || "request failed"}`);
  }
  remember(key);
  return data;
}

function rowsLabel(table) {
  const n = table.row_count ?? table.row_estimate;
  // A source that cannot estimate reports -1.
  return n === null || n === undefined || n < 0 ? "" :`${table.row_count == null ? "~" : ""}${n.toLocaleString()} rows`;
}

async function loadSources() {
  const nav = $("tables");
  nav.replaceChildren(el("p", { class: "muted" }, "Loading…"));
  try {
    const { sources } = await api("GET", "/olap/sources");
    nav.replaceChildren(
      ...(sources.length ? sources.map(sourceNode) : [el("p", { class: "muted" }, "No sources connected.")]),
    );
  } catch (e) {
    nav.replaceChildren(el("p", { class: "error" }, e.message));
  }
}

function sourceNode(source) {
  return el(
    "details",
    { open: "" },
    el("summary", {}, source.source, " ", el("span", { class: "muted" }, source.type)),
    ...source.tables.map(tableNode),
  );
}

function tableNode(table) {
  const columns = el("ul", { class: "cols" });
  const node = el(
    "details",
    {
      class: "table",
      ontoggle: () => {
        if (node.open && !columns.childElementCount) loadColumns(table.name, columns);
      },
    },
    el(
      "summary",
      {},
      table.name.slice(table.name.indexOf(".") + 1),
      " ",
      el("span", { class: "muted" }, rowsLabel(table)),
      table.loaded ? el("span", { class: "badge" }, "loaded") : null,
    ),
    el(
      "button",
      {
        type: "button",
        class: "link",
        onclick: () => {
          setMode("sql");
          $("input").value = `SELECT * FROM ${table.name} LIMIT 100`;
          $("input").focus();
        },
      },
      "Query this table",
    ),
    columns,
  );
  return node;
}

async function loadColumns(name, list) {
  list.replaceChildren(el("li", { class: "muted" }, "Loading…"));
  try {
    const schema = await api("GET", `/olap/tables/${encodeURIComponent(name)}/schema`);
    const keys = new Set(schema.primary_key || []);
    list.replaceChildren(
      ...(schema.columns || []).map((c) =>
        el("li", {}, c.name, keys.has(c.name) ? el("span", { class: "badge" }, "key") : null, " ",
          el("span", { class: "muted" }, c.type)),
      ),
    );
  } catch (e) {
    list.replaceChildren(el("li", { class: "error" }, e.message));
  }
}

function setMode(next) {
  mode = next;
  document.querySelector(`input[name="mode"][value="${next}"]`).checked = true;
  $("input").placeholder =
    next === "sql"
      ? "SELECT segment, count(*) FROM db.users GROUP BY 1"
      : "Ask in words, using the certified business terms";
}

async function run() {
  const text = $("input").value.trim();
  if (!text) return;
  $("run").disabled = true;
  $("status").textContent = "Running…";
  $("notes").replaceChildren();
  $("result").replaceChildren();
  rememberQuery(mode, text);
  try {
    const data =
      mode === "sql"
        ? await api("POST", "/olap/query", { sql: text })
        : await api("POST", "/olap/tools/answer_question", { question: text });
    showResult(data);
  } catch (e) {
    $("status").textContent = "";
    $("result").replaceChildren(el("pre", { class: "error" }, e.message));
  } finally {
    $("run").disabled = false;
  }
}

function note(title, text) {
  return el("details", {}, el("summary", {}, title), el("pre", {}, text));
}

function cell(value) {
  if (value === null || value === undefined) return el("td", { class: "null" }, "NULL");
  if (typeof value === "number") return el("td", { class: "num" }, String(value));
  return el("td", {}, typeof value === "object" ? JSON.stringify(value) : String(value));
}

function showResult(data) {
  const parts = [`${data.row_count} row${data.row_count === 1 ? "" : "s"}`];
  if (data.truncated) parts.push(`first ${data.rows.length} shown`);
  parts.push(`${data.execution_time_ms} ms`);
  if (data.from_cache) parts.push("from cache");
  $("status").textContent = parts.join(" · ");

  const notes = [];
  if (data.request) notes.push(note("Understood as", JSON.stringify(data.request, null, 2)));
  if (data.sql) notes.push(note("Compiled SQL", data.sql));
  if (data.literal_repairs) notes.push(note("Literal repairs", JSON.stringify(data.literal_repairs, null, 2)));
  if (data.grounding) notes.push(note("Join check", JSON.stringify(data.grounding, null, 2)));
  $("notes").replaceChildren(...notes);

  const columns = data.columns || [];
  const rows = data.rows || [];
  const table = el(
    "table",
    {},
    el("thead", {}, el("tr", {}, ...columns.map((c) => el("th", { scope: "col" }, c)))),
    el("tbody", {}, ...rows.map((row) => el("tr", {}, ...columns.map((c) => cell(row[c]))))),
  );
  $("result").replaceChildren(...(columns.length ? resultViews(columns, rows, table) : [table]));
}

function showView(name) {
  for (const view of ["query", "catalog", "audit", "docs"]) {
    $(`view-${view}`).hidden = view !== name;
    $(`tab-${view}`).setAttribute("aria-selected", String(view === name));
  }
}

$("tab-query").addEventListener("click", () => showView("query"));
$("tab-catalog").addEventListener("click", () => showView("catalog"));
$("tab-audit").addEventListener("click", () => showView("audit"));
$("tab-docs").addEventListener("click", () => showView("docs"));
$("auth").addEventListener("submit", (e) => {
  e.preventDefault();
  loadSources();
});
$("run").addEventListener("click", run);
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    run();
  }
});
for (const radio of document.querySelectorAll('input[name="mode"]')) {
  radio.addEventListener("change", () => setMode(radio.value));
}
$("key").value = recalled();
setMode("sql");
loadSources();

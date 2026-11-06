"use strict";
// The catalog review: what `trio catalog review` does, for a reviewer in a browser.
// Uses el, api and $ from console.js.

// canEdit and canCreate are what /ui/access says the key may do: a hint for the page, the routes still check.
const review = { list: null, connected: [], name: null, info: null, table: null, lang: "en", canEdit: false, canCreate: false };
const STATUS_LABEL = { modified: "edited" };

function catalogPath(...parts) {
  return `${API_PREFIX}/catalog/${parts.map(encodeURIComponent).join("/")}`;
}

function badge(status) {
  return el("span", { class: `badge status-${status}` }, STATUS_LABEL[status] || status);
}

function say(text, isError = false) {
  $("catalog-status").textContent = text;
  $("catalog-status").className = isError ? "error" : "muted";
}

// Shows the seconds as they pass: an analysis waits on the model for a minute or more.
async function act(label, work) {
  const started = Date.now();
  say(`${label}…`);
  const timer = setInterval(() => say(`${label}… ${Math.round((Date.now() - started) / 1000)} s`), 1000);
  try {
    const result = await work();
    say(`${label}: done.`);
    return result ?? true;
  } catch (e) {
    say(e.message, true);
    return false;
  } finally {
    clearInterval(timer);
  }
}

function words(text) {
  return text.split(",").map((w) => w.trim()).filter(Boolean);
}

// Saves on change (when the field loses focus), so tabbing through a form never re-renders it.
function field(label, value, save, { multiline = false, disabled = false } = {}) {
  const input = el(multiline ? "textarea" : "input", {
    class: "field",
    "aria-label": label,
    rows: multiline ? "2" : null,
    disabled,
  });
  input.value = value ?? "";
  input.addEventListener("change", () => save(input.value));
  return input;
}

async function loadCatalogs() {
  $("catalogs").replaceChildren(el("p", { class: "muted" }, "Loading…"));
  try {
    const access = await api("GET", "/ui/access");
    review.canEdit = access.update;
    review.canCreate = access.create;
    const listed = await api("GET", `${API_PREFIX}/catalog`);
    review.list = listed.catalogs;
    review.connected = listed.connected || [];
    if (review.name) await openCatalog(review.name);
    else renderNav();
  } catch (e) {
    review.list = null;
    $("catalogs").replaceChildren(el("p", { class: "error" }, e.message));
  }
}

function renderNav() {
  const items = review.list.map((c) => {
    const open = c.database_name === review.name && review.info;
    return el(
      "div",
      {},
      el(
        "button",
        { type: "button", class: open ? "item current" : "item", onclick: () => openCatalog(c.database_name) },
        c.database_name,
        " ",
        badge(open ? review.info.status : c.status),
      ),
      open
        ? el(
            "ul",
            { class: "cols" },
            el(
              "li",
              {},
              el("button", { type: "button", class: review.table === null ? "link current" : "link", onclick: openOntology }, "Ontology overview"),
            ),
            ...review.info.tables.map((t) =>
              el(
                "li",
                {},
                el(
                  "button",
                  {
                    type: "button",
                    class: t.table_name === review.table ? "link current" : "link",
                    onclick: () => openTable(t.table_name),
                  },
                  t.table_name,
                ),
                badge(t.review_status),
              ),
            ),
          )
        : null,
    );
  });
  const analyze =
    review.canCreate && review.connected.length
      ? el("button", { type: "button", class: review.analyzing ? "item current" : "item", onclick: analyzeView }, "Analyze a database")
      : null;
  $("catalogs").replaceChildren(
    ...(items.length ? items : [el("p", { class: "muted" }, review.canCreate ? "No catalogs yet." : "No catalogs yet: `trio catalog analyze` writes one.")]),
    analyze,
  );
}

async function openCatalog(name) {
  if (name !== review.name) review.table = null;
  review.analyzing = undefined;
  review.name = name;
  try {
    review.info = await api("GET", catalogPath(name, "draft"));
    if (!review.info.languages.includes(review.lang)) review.lang = review.info.languages[0] || "en";
    renderNav();
    renderHead();
    if (review.table) await openTable(review.table);
    else await openOntology();
  } catch (e) {
    say(e.message, true);
  }
}

// After an edit: the table's status changed, but the form stays as the reviewer left it.
async function refreshStatus() {
  review.info = await api("GET", catalogPath(review.name, "draft"));
  renderNav();
  renderHead();
  const table = review.info.tables.find((t) => t.table_name === review.table);
  if (table) $("table-badge")?.replaceChildren(badge(table.review_status));
}

function renderHead() {
  const info = review.info;
  const counts = Object.entries(info.review_summary)
    .map(([status, n]) => `${n} ${STATUS_LABEL[status] || status}`)
    .join(", ");
  const language = el(
    "select",
    { "aria-label": "Language", onchange: (e) => { review.lang = e.target.value; openCatalog(info.database_name); } },
    ...info.languages.map((l) => el("option", { value: l }, l)),
  );
  language.value = review.lang;
  $("catalog-head").replaceChildren(
    el(
      "div",
      { class: "bar" },
      el("h2", {}, info.database_name),
      badge(info.status),
      el("span", { class: "muted" }, `${info.table_count} tables · ${counts}`),
      language,
      review.canEdit && info.status === "draft"
        ? el("button", { type: "button", class: "secondary", onclick: approveAll }, "Approve remaining tables")
        : null,
      review.canEdit
        ? el("button", { type: "button", class: "secondary", onclick: suggestMeasures }, "Suggest measures")
        : null,
      review.canEdit && info.uncertified_terms
        ? el(
            "button",
            { type: "button", class: "secondary", onclick: certifyAll },
            info.uncertified_terms === 1 ? "Certify the uncertified term" : `Certify all ${info.uncertified_terms} terms`,
          )
        : null,
    ),
    el(
      "p",
      { class: "muted" },
      review.canEdit
        ? "The engine reads catalogs when it starts: approvals and certified terms reach queries after a restart."
        : "This key can read the catalog but not change it: reviewing needs the update permission.",
    ),
  );
}

// Analysis runs inside the request; leaving the page while it runs abandons it.
function analyzeView() {
  review.name = review.info = review.table = null;
  review.analyzing = true;
  renderNav();
  const database = el("select", { "aria-label": "Database" }, ...review.connected.map((n) => el("option", { value: n }, n)));
  const tables = el("input", { class: "field", "aria-label": "Only these tables", placeholder: "every table" });
  const run = el("button", { type: "submit" }, "Analyze");
  const exists = () => review.list.find((c) => c.database_name === database.value);
  const note = el("p", { class: "muted" });
  const explain = () => {
    const c = exists();
    note.textContent = c
      ? `${c.database_name} already has ${c.status === "approved" ? "an approved" : "a draft"} catalog. Analysing it again re-opens it as a draft (every table back to pending) and keeps the edits reviewers made; tables you name are described afresh and the others stay as they are. What is live stays live until it is approved again.`
      : "The model describes every table and column (one call per table, often a minute or more). The result is a draft for review: nothing is live until it is approved.";
  };
  database.addEventListener("change", explain);
  explain();
  const form = el(
    "form",
    { class: "analyze" },
    el("label", {}, "Database", database),
    el("label", {}, "Only these tables (comma-separated)", tables),
    note,
    run,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    run.disabled = true;
    const body = { database: database.value };
    if (words(tables.value).length) body.tables = words(tables.value);
    const result = await act(`Analysing ${body.database}`, () => api("POST", `${API_PREFIX}/catalog/analyze`, body));
    run.disabled = false;
    if (!result) return;
    review.analyzing = undefined;
    await loadCatalogs();
    await openCatalog(result.database);
    const warnings = [
      result.failed_tables.length ? `could not read ${result.failed_tables.join(", ")}` : "",
      result.fallback_tables.length ? `no model descriptions for ${result.fallback_tables.join(", ")}` : "",
    ].filter(Boolean);
    const what = body.tables ? `Analysed ${body.tables.join(", ")}` : "Analysed every table";
    say(`${what}: the catalog has ${result.table_count} tables and is a ${result.status}.${warnings.length ? ` Note: ${warnings.join("; ")}.` : ""}`, warnings.length > 0);
  });
  $("catalog-head").replaceChildren(el("h2", {}, "Analyze a database"));
  say("");
  $("catalog-table").replaceChildren(form);
}

async function suggestMeasures() {
  const result = await act("Reading the query history and the views", () =>
    api("POST", catalogPath(review.name, "suggest"), {}),
  );
  if (!result) return;
  await openCatalog(review.name);
  const lines = [`Read ${result.statements} statement(s) and ${result.views} view(s).`];
  if (result.added.length) {
    lines.push(`${result.added.length} draft measure(s): ${result.added.map((a) => `${a.table}.${a.name}`).join(", ")}. Certify the right ones on each table's term.`);
  } else {
    lines.push("Nothing new to suggest.");
  }
  if (result.without_term.length) lines.push(`No term to hold expressions on: ${result.without_term.join(", ")}.`);
  if (result.unavailable.length) lines.push(`Not read: ${result.unavailable.join("; ")}`);
  say(lines.join(" "), !result.added.length && result.unavailable.length > 0);
}

async function approveAll() {
  const sure = confirm(
    "Approve every remaining table? Their business terms stay uncertified; approve tables one by one to certify them.",
  );
  if (sure && (await act("Approving the catalog", () => api("POST", catalogPath(review.name, "approve"), {})))) {
    await openCatalog(review.name);
  }
}

async function certifyAll() {
  const n = review.info.uncertified_terms;
  const sure = confirm(
    `Certify ${n === 1 ? "the uncertified term" : `all ${n} uncertified terms`} without reviewing them one by one? A certified term decides what a question means.`,
  );
  if (sure && (await act("Certifying the terms", () => api("POST", catalogPath(review.name, "terms", "certify"))))) {
    await openCatalog(review.name);
  }
}

async function approveTable(name) {
  const path = `${catalogPath(review.name, "approve", name)}?certify_term=true`;
  if (await act(`Approving ${name}`, () => api("POST", path))) await openCatalog(review.name);
}

async function openTable(name) {
  review.table = name;
  renderNav();
  try {
    const table = await api("GET", catalogPath(review.name, "draft", "tables", name));
    $("catalog-table").replaceChildren(...tableView(table).filter(Boolean));
  } catch (e) {
    $("catalog-table").replaceChildren(el("p", { class: "error" }, e.message));
  }
}

function tableView(t) {
  const lang = review.lang;
  const draft = review.info.status === "draft";
  const editable = draft && review.canEdit;
  const path = catalogPath(review.name, "draft", "tables", t.table_name);
  const edit = async (url, body) => {
    const query = `?lang=${encodeURIComponent(lang)}`;
    if (await act("Saving", () => api("PATCH", url + query, body))) await refreshStatus();
  };
  const rows = t.row_count != null && t.row_count >= 0 ? ` · ${t.row_count.toLocaleString()} rows` : "";
  return [
    el(
      "div",
      { class: "bar" },
      el("h3", {}, t.table_name),
      el("span", { id: "table-badge" }, badge(t.review_status)),
      el("span", { class: "muted" }, `${t.column_count} columns${rows}`),
      review.canEdit
        ? el(
            "button",
            { type: "button", onclick: () => approveTable(t.table_name) },
            t.terms.length ? "Approve table and certify its terms" : "Approve table",
          )
        : null,
    ),
    draft || !review.canEdit
      ? null
      : el("p", { class: "muted" }, "This catalog is approved, so its descriptions are read-only. Terms can still be edited and expressions certified."),
    el(
      "div",
      { class: "form" },
      el("label", {}, "Name", field("Table name", t.human_name[lang], (v) => edit(path, { human_name: { [lang]: v } }), { disabled: !editable })),
      el(
        "label",
        {},
        "Description",
        field("Table description", t.description[lang], (v) => edit(path, { description: { [lang]: v } }), {
          multiline: true,
          disabled: !editable,
        }),
      ),
    ),
    el("h4", {}, "Columns"),
    el("p", { class: "muted" }, "The semantic type decides masking: masking rules are written per semantic type."),
    el(
      "div",
      { class: "scroll" },
      el(
        "table",
        { class: "review" },
        el("thead", {}, el("tr", {}, ...["Column", "Type", "Semantic type", "Description", "Samples"].map((h) => el("th", { scope: "col" }, h)))),
        el("tbody", {}, ...t.columns.map((c) => columnRow(c, `${path}/columns/${encodeURIComponent(c.name)}`, edit, editable))),
      ),
    ),
    t.relationships.length ? el("h4", {}, "Relationships") : null,
    t.relationships.length
      ? el(
          "ul",
          { class: "plain" },
          ...t.relationships.map((r) =>
            el(
              "li",
              {},
              `${r.source_column} → ${r.target_table}.${r.target_column} `,
              el("span", { class: "muted" }, r.relationship_type),
              verdict(r.verified),
            ),
          ),
        )
      : null,
    el("h4", {}, "Business terms"),
    t.terms.length ? null : el("p", { class: "muted" }, "No term covers this table."),
    ...t.terms.map(termView),
  ];
}

// Only a relationship the data bears out is used to join tables.
function verdict(verified) {
  if (verified === true) return el("span", { class: "badge status-approved" }, "verified by the data");
  if (verified === false) return el("span", { class: "badge status-modified" }, "contradicted by the data");
  return el("span", { class: "badge" }, "not checked: not used for joins");
}

function columnRow(c, path, edit, editable) {
  const lang = review.lang;
  return el(
    "tr",
    {},
    el(
      "td",
      {},
      c.name,
      c.is_primary_key ? el("span", { class: "badge" }, "key") : null,
      c.references ? el("span", { class: "badge" }, `→ ${c.references}`) : null,
    ),
    el("td", { class: "muted" }, c.data_type),
    el("td", {}, field(`${c.name} semantic type`, c.semantic_type, (v) => edit(path, { semantic_type: v }), { disabled: !editable })),
    el("td", {}, field(`${c.name} description`, c.description[lang], (v) => edit(path, { description: { [lang]: v } }), { disabled: !editable })),
    el("td", { class: "muted" }, (c.sample_values || []).slice(0, 3).map(String).join(", ")),
  );
}

function termView(term) {
  const lang = review.lang;
  const path = catalogPath(review.name, "terms", term.name);
  const save = (body) => act(`Saving ${term.name}`, () => api("PATCH", path, body));
  const certify = async (name) => {
    if (await act(`Certifying ${name}`, () => api("PATCH", path, { certify_sql: [name] }))) await openTable(review.table);
  };
  return el(
    "div",
    { class: "term" },
    el(
      "div",
      { class: "bar" },
      el("strong", {}, term.name),
      term.certified ? el("span", { class: "badge status-approved" }, "certified") : el("span", { class: "badge status-pending" }, "not certified"),
      term.tables.length > 1 ? el("span", { class: "muted" }, `covers ${term.tables.join(", ")}`) : null,
    ),
    term.description[lang] ? el("p", {}, term.description[lang]) : null,
    el("label", {}, "Synonyms", field(`${term.name} synonyms`, term.synonyms.join(", "), (v) => save({ synonyms: words(v) }), { disabled: !review.canEdit })),
    ...term.columns.map((c) =>
      el(
        "label",
        {},
        `Words for ${c.column}`,
        field(`Words for ${c.column}`, c.synonyms.join(", "), (v) => save({ columns: { [c.column]: words(v) } }), { disabled: !review.canEdit }),
        Object.keys(c.values).length
          ? el("span", { class: "muted" }, "Values: " + Object.entries(c.values).map(([v, ws]) => (ws.length ? `${v} (${ws.join(", ")})` : v)).join("; "))
          : null,
      ),
    ),
    term.expressions.length
      ? el(
          "table",
          { class: "review" },
          el(
            "tbody",
            {},
            ...term.expressions.map((x) =>
              el(
                "tr",
                {},
                el("td", {}, x.name),
                el("td", {}, el("code", {}, x.expression)),
                el("td", { class: "muted" }, x.description[lang] || ""),
                el(
                  "td",
                  {},
                  x.certified
                    ? el("span", { class: "badge status-approved" }, "measure")
                    : review.canEdit
                      ? el("button", { type: "button", class: "secondary", onclick: () => certify(x.name) }, "Certify as measure")
                      : el("span", { class: "badge" }, "draft"),
                ),
              ),
            ),
          ),
        )
      : null,
  );
}

$("tab-catalog").addEventListener("click", () => {
  if (!review.list) loadCatalogs();
});
$("auth").addEventListener("submit", () => {
  review.list = null;
  if (!$("view-catalog").hidden) loadCatalogs();
});

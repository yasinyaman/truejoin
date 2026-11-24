"use strict";
// The catalog review: what `truejoin catalog review` does, for a reviewer in a browser.
// Uses el, api and $ from console.js.

// canEdit and canCreate are what /ui/access says the key may do: a hint for the page, the routes still check.
const review = { list: null, connected: [], name: null, info: null, table: null, lang: "en", canEdit: false, canCreate: false };
// A status is the server's word; the ones the review knows are shown in the reader's language.
const STATUS_LABEL = { pending: tr("Pending"), approved: tr("Approved"), modified: tr("Edited"), draft: tr("Draft") };

function catalogPath(...parts) {
  return `${API_PREFIX}/catalog/${parts.map(encodeURIComponent).join("/")}`;
}

function badge(status) {
  return el("span", { class: `badge status-${status}` }, STATUS_LABEL[status] || status);
}

// kind: true (or "error") for a failure, "warn" for something worth a look, else a plain note.
function say(text, kind = false) {
  $("catalog-status").textContent = text;
  $("catalog-status").className = kind === true ? "error" : kind || "muted";
}

// Shows the seconds as they pass: an analysis waits on the model for a minute or more.
async function act(label, work) {
  const started = Date.now();
  say(`${label}…`);
  const timer = setInterval(() => say(`${label}… ${Math.round((Date.now() - started) / 1000)} s`), 1000);
  try {
    const result = await work();
    say(tr`${label}: done.`);
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
// A locked field is read-only, not disabled: its text stays readable (and can be
// copied), where a disabled field greys it out like a placeholder.
function field(label, value, save, { multiline = false, disabled = false } = {}) {
  const input = el(multiline ? "textarea" : "input", {
    class: "field",
    "aria-label": label,
    rows: multiline ? "2" : null,
    readonly: disabled,
  });
  input.value = value ?? "";
  input.addEventListener("change", () => save(input.value));
  return input;
}

async function loadCatalogs() {
  $("catalogs").replaceChildren(el("p", { class: "muted" }, tr("Loading…")));
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
              el("button", { type: "button", class: review.table === null ? "link current" : "link", onclick: openOntology }, tr("Ontology overview")),
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
      ? el("button", { type: "button", class: review.analyzing ? "item current" : "item", onclick: analyzeView }, tr("Analyze a database"))
      : null;
  const importing = review.canCreate
    ? el("button", { type: "button", class: review.importing ? "item current" : "item", onclick: importView }, tr("Import a catalog"))
    : null;
  $("catalogs").replaceChildren(
    ...(items.length ? items : [el("p", { class: "muted" }, review.canCreate ? tr("No catalogs yet.") : tr("No catalogs yet: `truejoin catalog analyze` writes one."))]),
    analyze,
    importing,
  );
}

// The semantic types offered as a type is typed: the known ones, this catalog's,
// and those a masking rule names, each saying what it does. Only for an editor.
async function loadTypes(name) {
  let types = [];
  if (review.canEdit) {
    try {
      types = (await api("GET", catalogPath(name, "semantic-types"))).types;
    } catch {}
  }
  const list = $("semantic-types") || document.body.appendChild(el("datalist", { id: "semantic-types" }));
  list.replaceChildren(
    ...types.map((t) => el("option", { value: t.name, label: [t.personal ? tr("Personal") : "", t.masked_by ? tr`masked: ${t.masked_by}` : ""].filter(Boolean).join(" · ") || null })),
  );
}

function typeField(input) {
  input.setAttribute("list", "semantic-types");
  return input;
}

async function openCatalog(name) {
  review.importing = false;
  if (name !== review.name) review.table = null;
  if (review.analyzing) say(""); // the analysis line belongs to the page being left
  review.analyzing = undefined;
  review.name = name;
  try {
    review.info = await api("GET", catalogPath(name, "draft"));
    if (!review.info.languages.includes(review.lang)) review.lang = review.info.languages[0] || "en";
    review.missing = await missingLanguages();
    await loadTypes(name);
    renderNav();
    renderHead();
    if (review.table) await openTable(review.table);
    else await openOntology();
  } catch (e) {
    say(e.message, true);
  }
}

// The languages set after this catalog was written: it has no words in them
// until an analysis writes some. Known only to a key that may read the settings.
async function missingLanguages() {
  try {
    const rows = (await api("GET", `${API_PREFIX}/settings`)).settings || [];
    const wanted = rows.find((r) => r.path === "settings.i18n.languages")?.value || [];
    return wanted.filter((l) => !review.info.languages.includes(l));
  } catch {
    return []; // not an administrator, or no settings kept: nothing to compare with
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
    { "aria-label": tr("Language"), onchange: (e) => { review.lang = e.target.value; openCatalog(info.database_name); } },
    ...info.languages.map((l) => el("option", { value: l }, l)),
  );
  language.value = review.lang;
  $("catalog-head").replaceChildren(
    el(
      "div",
      { class: "bar" },
      el("h2", {}, info.database_name),
      badge(info.status),
      el("span", { class: "muted" }, tr`${info.table_count} tables · ${counts}`),
      language,
      review.canEdit && info.status === "draft"
        ? el("button", { type: "button", class: "secondary", onclick: approveAll }, tr("Approve remaining tables"))
        : null,
      review.canEdit && info.status === "approved"
        ? el("button", { type: "button", class: "secondary", onclick: reopen }, tr("Re-open for review"))
        : null,
      el(
        "select",
        {
          "aria-label": tr("Export the catalog"),
          class: "secondary",
          onchange: (e) => {
            if (e.target.value) downloadExport(e.target.value);
            e.target.value = "";
          },
        },
        el("option", { value: "" }, tr("Export…")),
        el("option", { value: "json" }, tr("JSON (re-importable)")),
        el("option", { value: "yaml" }, tr("YAML (re-importable)")),
        el("option", { value: "markdown" }, "Markdown"),
        el("option", { value: "ossie" }, "Apache Ossie semantic model"),
      ),
      review.canEdit
        ? el("button", { type: "button", class: "secondary", onclick: suggestMeasures }, tr("Suggest measures"))
        : null,
      review.canEdit && info.uncertified_terms
        ? el(
            "button",
            { type: "button", class: "secondary", onclick: certifyAll },
            info.uncertified_terms === 1 ? tr("Certify the uncertified term") : tr`Certify all ${info.uncertified_terms} terms`,
          )
        : null,
    ),
    el(
      "p",
      { class: "muted" },
      review.canEdit
        ? tr("An approved catalog is live at once: its masks, verified joins and certified terms. Changes made with `truejoin catalog` are picked up within 30 seconds; a draft changes nothing until it is approved.")
        : tr("This key can read the catalog but not change it: reviewing needs the update permission."),
    ),
    ...(review.missing?.length
      ? [el("p", { class: "warn" }, tr`This catalog was written in ${info.languages.join(", ")}. It has no words in ${review.missing.join(", ")} yet: analyze it again to draft them. Certified terms keep their words; add to those one by one.`)]
      : []),
  );
}

// Analysis runs inside the request; leaving the page while it runs abandons it.
const JOBS = `${API_PREFIX}/catalog/analyze/jobs`;

// An analysis runs on the server as a job; this polls it, so a long one never meets a proxy timeout.
async function followJob(job, view) {
  const started = Date.parse(job.started_at);
  const label = tr`Analysing ${job.tables ? job.tables.join(", ") : tr("every table")} of ${job.database}`;
  while (job.state === "running") {
    if (review.analyzing === view) say(`${label}… ${Math.round((Date.now() - started) / 1000)} s`);
    await new Promise((done) => setTimeout(done, 2000));
    job = await api("GET", `${JOBS}/${encodeURIComponent(job.id)}`);
  }
  return job;
}

// view is the analyze page this was started from: leaving it, or opening it again, ends what this one shows.
async function showJob(job, run, view) {
  run.disabled = true;
  try {
    job = await followJob(job, view);
  } catch (e) {
    if (review.analyzing === view) say(e.message, true);
    return;
  } finally {
    run.disabled = false;
  }
  if (review.analyzing !== view) return; // the reviewer moved on; the catalog list shows the result
  if (job.state === "failed") return say(tr`Analysing ${job.database} failed: ${job.error}`, true);
  const result = job.result;
  review.analyzing = undefined;
  await loadCatalogs();
  await openCatalog(result.database);
  const warnings = [
    result.failed_tables.length ? tr`could not read ${result.failed_tables.join(", ")}` : "",
    result.fallback_tables.length ? tr`no model descriptions for ${result.fallback_tables.join(", ")}` : "",
  ].filter(Boolean);
  const what = job.tables ? tr`Analysed ${job.tables.join(", ")}` : tr("Analysed every table");
  say(tr`${what}: the catalog has ${result.table_count} tables and is a ${result.status}.${warnings.length ? tr` Note: ${warnings.join("; ")}.` : ""}`, warnings.length ? "warn" : false);
}

// A whole catalog (export?lang=all) from another install; it arrives as an unreviewed draft.
function importView() {
  review.name = review.info = review.table = null;
  review.analyzing = undefined;
  review.importing = true;
  renderNav();
  const name = el("input", { class: "field", "aria-label": tr("Database name"), list: "import-names", placeholder: tr("the database it describes here") });
  const names = el("datalist", { id: "import-names" }, ...review.connected.map((n) => el("option", { value: n })));
  const file = el("input", { type: "file", accept: ".json,.yaml,.yml", "aria-label": tr("Catalog file") });
  const replace = el("input", { type: "checkbox" });
  const submit = el("button", { type: "submit" }, tr("Import"));
  const form = el(
    "form",
    { class: "analyze" },
    el("label", {}, tr("Database"), name, names),
    el("label", {}, tr("Catalog file (JSON or YAML, exported with every language)"), file),
    el("label", { class: "check" }, replace, tr(" Replace the catalog this database has")),
    el("p", { class: "muted" }, tr("It is stored as a draft with every table pending and nothing certified: what another install approved is reviewed again here before anything goes live.")),
    submit,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const target = name.value.trim();
    if (!target || !file.files.length) return say(tr("Name the database and pick a file."), true);
    const text = await file.files[0].text();
    submit.disabled = true;
    await freshToken();
    const headers = withAuth({ "Content-Type": "application/octet-stream" });
    const done = await act(tr`Importing into ${target}`, async () => {
      const response = await fetch(`${catalogPath(target, "import")}?replace=${replace.checked}`, { method: "POST", headers, body: text });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || response.statusText));
      return data;
    });
    submit.disabled = false;
    if (!done) return;
    await loadCatalogs();
    await openCatalog(done.database);
    say(tr`Imported ${done.tables} tables and ${done.terms} terms into ${done.database}, as a draft to review.`);
  });
  $("catalog-head").replaceChildren(el("h2", {}, tr("Import a catalog")));
  say("");
  $("catalog-table").replaceChildren(form);
}

// The catalog as a file; fetched with the key, which a plain link could not send.
async function downloadExport(format) {
  await freshToken();
  const headers = withAuth();
  await act(tr`Exporting ${review.name} as ${format}`, async () => {
    const response = await fetch(`${catalogPath(review.name, "export")}?format=${format}&lang=all`, { headers });
    if (!response.ok) throw new Error(response.statusText || tr("The request failed"));
    const url = URL.createObjectURL(await response.blob());
    const extension = { markdown: "md", ossie: "ossie.yaml" }[format] || format;
    const link = el("a", { href: url, download: `${review.name}_catalog.${extension}` });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
}

function analyzeView() {
  review.name = review.info = review.table = null;
  review.importing = false;
  const view = (review.analyzing = {});
  renderNav();
  const database = el("select", { "aria-label": tr("Database") }, ...review.connected.map((n) => el("option", { value: n }, n)));
  const tables = el("input", { class: "field", "aria-label": tr("Only these tables"), placeholder: tr("every table") });
  const run = el("button", { type: "submit" }, tr("Analyze"));
  const exists = () => review.list.find((c) => c.database_name === database.value);
  const note = el("p", { class: "muted" });
  const explain = () => {
    const c = exists();
    note.textContent = c
      ? tr`${c.database_name} already has ${c.status === "approved" ? tr("an approved") : tr("a draft")} catalog. Analysing it again re-opens it as a draft and keeps the edits reviewers made. Tables you name are described afresh and go back to pending; the others stay as they are, review included. What is live stays live until it is approved again.`
      : tr("The model describes every table and column (one call per table, often a minute or more). The result is a draft for review: nothing is live until it is approved.");
  };
  database.addEventListener("change", explain);
  explain();
  const form = el(
    "form",
    { class: "analyze" },
    el("label", {}, tr("Database"), database),
    el("label", {}, tr("Only these tables (comma-separated)"), tables),
    note,
    run,
    el("p", { class: "muted" }, tr("It runs on the server: you can leave this page and come back to it.")),
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = { database: database.value };
    if (words(tables.value).length) body.tables = words(tables.value);
    run.disabled = true;
    try {
      const job = await api("POST", JOBS, body);
      await showJob(job, run, view);
    } catch (e) {
      say(e.message, true);
      run.disabled = false;
    }
  });
  $("catalog-head").replaceChildren(el("h2", {}, tr("Analyze a database")));
  say("");
  $("catalog-table").replaceChildren(form);
  // One already running (started here earlier, or by someone else): follow it.
  api("GET", JOBS).then(
    (jobs) => {
      const job = jobs.find((j) => j.state === "running");
      if (!job || review.analyzing !== view) return;
      database.value = job.database;
      explain();
      showJob(job, run, view);
    },
    () => {},
  );
}

async function suggestMeasures() {
  const result = await act(tr("Reading the query history and the views"), () =>
    api("POST", catalogPath(review.name, "suggest"), {}),
  );
  if (!result) return;
  await openCatalog(review.name);
  const lines = [tr`Read ${result.statements} statement(s) and ${result.views} view(s).`];
  if (result.added.length) {
    lines.push(tr`${result.added.length} draft measure(s): ${result.added.map((a) => `${a.table}.${a.name}`).join(", ")}. Certify the right ones on each table's term.`);
  } else {
    lines.push(tr("Nothing new to suggest."));
  }
  if (result.without_term.length) lines.push(tr`No term to hold expressions on: ${result.without_term.join(", ")}.`);
  if (result.unavailable.length) lines.push(tr`Not read: ${result.unavailable.join("; ")}`);
  say(lines.join(" "), result.unavailable.length ? "warn" : false);
}

async function setOwner(body) {
  await act(tr("Saving the owner"), () => api("PATCH", catalogPath(review.name, "owners"), body));
}

async function reopen() {
  const sure = await ask(
    tr`Re-open ${review.name} as a draft to edit it? Every table goes back to pending. What is live (masks, joins, certified terms) stays live until the catalog is approved again.`,
  );
  if (sure && (await act(tr("Re-opening the catalog"), () => api("POST", catalogPath(review.name, "reopen"), {})))) {
    await openCatalog(review.name);
  }
}

async function approveAll() {
  const sure = await ask(
    tr("Approve every remaining table? Their business terms stay uncertified; approve tables one by one to certify them."),
  );
  if (sure && (await act(tr("Approving the catalog"), () => api("POST", catalogPath(review.name, "approve"), {})))) {
    await openCatalog(review.name);
  }
}

async function certifyAll() {
  const n = review.info.uncertified_terms;
  const sure = await ask(
    tr`Certify ${n === 1 ? tr("the uncertified term") : tr`all ${n} uncertified terms`} without reviewing them one by one? A certified term decides what a question means.`,
  );
  if (sure && (await act(tr("Certifying the terms"), () => api("POST", catalogPath(review.name, "terms", "certify"))))) {
    await openCatalog(review.name);
  }
}

async function approveTable(name) {
  const path = `${catalogPath(review.name, "approve", name)}?certify_term=true`;
  if (await act(tr`Approving ${name}`, () => api("POST", path))) await openCatalog(review.name);
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
    const saved = await act(tr("Saving"), () => api("PATCH", url + query, body));
    if (!saved) return;
    await refreshStatus();
    // A type no masking rule names is saved, and said: its values would not be masked.
    if (saved.warning) say(saved.warning, "warn");
  };
  const rows = t.row_count != null && t.row_count >= 0 ? tr` · ~${t.row_count.toLocaleString()} rows` : "";
  return [
    el(
      "div",
      { class: "bar" },
      el("h3", {}, t.table_name),
      el("span", { id: "table-badge" }, badge(t.review_status)),
      el("span", { class: "muted" }, tr`${t.column_count} columns${rows}`),
      // Only a draft is reviewed; an approved table is offered again only for
      // the terms it still has to certify.
      editable && (t.review_status !== "approved" || t.terms.some((term) => !term.certified))
        ? el(
            "button",
            { type: "button", onclick: () => approveTable(t.table_name) },
            t.terms.length ? tr("Approve table and certify its terms") : tr("Approve table"),
          )
        : null,
    ),
    draft || !review.canEdit
      ? null
      : el("p", { class: "muted" }, tr("This catalog is approved, so its descriptions are read-only: re-open it for review to change them. Terms can still be edited and expressions certified.")),
    el(
      "div",
      { class: "form" },
      // Not part of the review: an approved catalog takes it too.
      el("label", {}, tr("Owner"), field(tr("Table owner"), t.owner, (v) => setOwner({ tables: { [t.table_name]: v } }), { disabled: !review.canEdit })),
      el("label", {}, tr("Name"), field(tr("Table name"), t.human_name[lang], (v) => edit(path, { human_name: { [lang]: v } }), { disabled: !editable })),
      el(
        "label",
        {},
        tr("Description"),
        field(tr("Table description"), t.description[lang], (v) => edit(path, { description: { [lang]: v } }), {
          multiline: true,
          disabled: !editable,
        }),
      ),
    ),
    el("h4", {}, tr("Columns")),
    el("p", { class: "muted" }, tr("The semantic type decides masking: masking rules are written per semantic type.")),
    el(
      "div",
      { class: "scroll" },
      el(
        "table",
        { class: "review" },
        el("thead", {}, el("tr", {}, ...[tr("Column"), tr("Type"), tr("Semantic type"), tr("Description"), tr("Samples")].map((h) => el("th", { scope: "col" }, h)))),
        el("tbody", {}, ...t.columns.map((c) => columnRow(c, `${path}/columns/${encodeURIComponent(c.name)}`, edit, editable))),
      ),
    ),
    t.relationships.length ? el("h4", {}, tr("Relationships")) : null,
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
    el("h4", {}, tr("Business terms")),
    t.terms.length ? null : el("p", { class: "muted" }, tr("No term covers this table.")),
    ...t.terms.map(termView),
  ];
}

// Only a relationship the data bears out is used to join tables.
function verdict(verified) {
  if (verified === true) return el("span", { class: "badge status-approved" }, tr("verified by the data"));
  if (verified === false) return el("span", { class: "badge status-modified" }, tr("contradicted by the data"));
  return el("span", { class: "badge" }, tr("not checked: not used for joins"));
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
      c.is_primary_key ? el("span", { class: "badge" }, tr("Key")) : null,
      c.references ? el("span", { class: "badge" }, `→ ${c.references}`) : null,
    ),
    el("td", { class: "muted" }, c.data_type),
    el("td", {}, typeField(field(tr`${c.name} semantic type`, c.semantic_type, (v) => edit(path, { semantic_type: v }), { disabled: !editable }))),
    el("td", {}, field(tr`${c.name} description`, c.description[lang], (v) => edit(path, { description: { [lang]: v } }), { disabled: !editable })),
    el("td", { class: "muted" }, (c.sample_values || []).slice(0, 3).map(String).join(", ")),
  );
}

function termView(term) {
  const lang = review.lang;
  const path = catalogPath(review.name, "terms", term.name);
  const save = (body) => act(tr`Saving ${term.name}`, () => api("PATCH", path, body));
  const certify = async (name) => {
    if (await act(tr`Certifying ${name}`, () => api("PATCH", path, { certify_sql: [name] }))) await openTable(review.table);
  };
  const remove = async (x) => {
    const what = x.certified ? tr`the measure ${x.name}: questions stop using it at once` : tr`the draft ${x.name}`;
    if ((await ask(tr`Remove ${what}?`, { yes: tr("Remove") })) && (await act(tr`Removing ${x.name}`, () => api("DELETE", `${path}/measures/${encodeURIComponent(x.name)}`)))) {
      await openTable(review.table);
    }
  };
  return el(
    "div",
    { class: "term" },
    el(
      "div",
      { class: "bar" },
      el("strong", {}, term.name),
      term.certified ? el("span", { class: "badge status-approved" }, tr("Certified")) : el("span", { class: "badge status-pending" }, tr("not certified")),
      term.tables.length > 1 ? el("span", { class: "muted" }, tr`covers ${term.tables.join(", ")}`) : null,
    ),
    term.description[lang] ? el("p", {}, term.description[lang]) : null,
    el("label", {}, tr("Owner"), field(tr`${term.name} owner`, term.owner, (v) => setOwner({ terms: { [term.name]: v } }), { disabled: !review.canEdit })),
    el("label", {}, tr("Synonyms"), field(tr`${term.name} synonyms`, term.synonyms.join(", "), (v) => save({ synonyms: words(v) }), { disabled: !review.canEdit })),
    ...term.columns.map((c) =>
      el(
        "label",
        {},
        tr`Words for ${c.column}`,
        field(tr`Words for ${c.column}`, c.synonyms.join(", "), (v) => save({ columns: { [c.column]: words(v) } }), { disabled: !review.canEdit }),
        Object.keys(c.values).length
          ? el("span", { class: "muted" }, tr("Values: ") + Object.entries(c.values).map(([v, ws]) => (ws.length ? `${v} (${ws.join(", ")})` : v)).join("; "))
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
                  { class: "nowrap" },
                  x.certified
                    ? el("span", { class: "badge status-approved" }, tr("Measure"))
                    : review.canEdit
                      ? el("button", { type: "button", class: "secondary", onclick: () => certify(x.name) }, tr("Certify as measure"))
                      : el("span", { class: "badge" }, tr("Draft")),
                  review.canEdit
                    ? el("button", { type: "button", class: "secondary", "aria-label": tr`Remove ${x.name}`, onclick: () => remove(x) }, tr("Remove"))
                    : null,
                ),
              ),
            ),
          ),
        )
      : null,
    review.canEdit ? measureForm(term, path) : null,
  );
}

// A measure written by hand: checked on the server (shape, then a trial sum in DuckDB), stored as a draft.
function measureForm(term, path) {
  const name = el("input", { class: "field", "aria-label": tr("Measure name"), placeholder: "net_amount" });
  const expression = el("textarea", { class: "field", rows: "2", spellcheck: "false", "aria-label": tr("Expression"), placeholder: "amount - discount" });
  const description = el("input", { class: "field", "aria-label": tr("Measure description"), placeholder: tr`what it means, in ${review.lang}` });
  const verdict = el("p", { class: "muted", "aria-live": "polite" });
  const show = (text, kind = "muted") => {
    verdict.textContent = text;
    verdict.className = kind;
  };
  const check = el("button", { type: "button", class: "secondary" }, tr("Check"));
  const add = el("button", { type: "submit" }, tr("Add as draft"));
  check.addEventListener("click", async () => {
    try {
      const r = await api("POST", `${path}/measures/check`, { table: review.table, expression: expression.value });
      show(tr`Usable: ${r.expression} (reads ${r.reads.join(", ")}).`);
    } catch (e) {
      show(e.message, "error");
    }
  });
  const form = el(
    "form",
    { class: "analyze" },
    el("label", {}, tr("Name"), name),
    el("label", {}, tr`Expression over ${review.table}'s columns: what goes inside the sum, not the sum`, expression),
    el("label", {}, tr("Description"), description),
    el("div", { class: "bar" }, check, add),
    verdict,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    add.disabled = true;
    try {
      const body = { table: review.table, name: name.value, expression: expression.value, description: description.value, lang: review.lang };
      const r = await api("POST", `${path}/measures`, body);
      await openTable(review.table);
      say(tr`Added ${r.name} as a draft: certify it to make it a measure.`);
    } catch (e) {
      show(e.message, "error");
    } finally {
      add.disabled = false;
    }
  });
  return el("details", { class: "try" }, el("summary", {}, tr("Add a measure")), form);
}

// Search: every word, across the catalogs; a result opens where it lives.
const KIND = { table: tr("Table"), column: tr("Column"), term: tr("Term"), measure: tr("Measure"), abbreviation: tr("Abbreviation") };
let searchTimer;

async function runSearch() {
  const q = $("catalog-q").value.trim();
  if (!q) {
    if (review.searching) {
      review.searching = false;
      if (review.name) await openCatalog(review.name);
      else $("catalog-table").replaceChildren(el("p", { class: "muted empty" }, tr("Pick a table to review.")));
    }
    return;
  }
  review.searching = true;
  $("catalog-head").replaceChildren(el("h2", {}, tr("Search")));
  say("");
  try {
    const r = await api("GET", `${API_PREFIX}/catalog/search?q=${encodeURIComponent(q)}&lang=${encodeURIComponent(review.lang)}`);
    if ($("catalog-q").value.trim() !== q) return; // a newer search is on its way
    $("catalog-table").replaceChildren(searchView(r));
  } catch (e) {
    say(e.message, true);
  }
}

// Puts one tag on the tables and columns a search found, catalog by catalog (each has to be a draft).
function tagResultsForm(hits) {
  const taggable = hits.filter((h) => h.kind === "table" || h.kind === "column");
  if (!review.canEdit || !taggable.length) return null;
  const name = el("input", { class: "field", "aria-label": tr("Tag"), placeholder: "finance" });
  const verdict = el("p", { class: "muted", "aria-live": "polite" });
  const form = el(
    "form",
    { class: "bar" },
    el("label", {}, tr`Tag the ${taggable.length} table(s) and column(s) found`, name),
    el("button", { type: "submit", class: "secondary" }, tr("Tag them")),
    verdict,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const tag = name.value.trim();
    if (!tag) return;
    const byCatalog = new Map();
    for (const h of taggable) {
      if (!byCatalog.has(h.database)) byCatalog.set(h.database, []);
      byCatalog.get(h.database).push(h.kind === "table" ? { table: h.table } : { table: h.table, column: h.name });
    }
    const said = [];
    for (const [database, targets] of byCatalog) {
      try {
        const r = await api("POST", catalogPath(database, "tags", tag), { targets });
        said.push(tr`${database}: ${r.changed} tagged`);
      } catch (e) {
        said.push(`${database}: ${e.message}`);
      }
    }
    verdict.textContent = said.join(" · ");
  });
  return form;
}

function searchView(r) {
  if (!r.hits.length) {
    return el("p", { class: "muted" }, tr("Nothing found: every word has to be in a name, a synonym, a tag or a description."));
  }
  return el(
    "div",
    {},
    tagResultsForm(r.hits),
    el(
    "div",
    { class: "scroll" },
    el(
      "table",
      { class: "review" },
      el("thead", {}, el("tr", {}, ...[tr("What"), tr("Where"), tr("Name"), tr("Says")].map((h) => el("th", { scope: "col" }, h)))),
      el(
        "tbody",
        {},
        ...r.hits.map((h) =>
          el(
            "tr",
            {},
            el("td", {}, KIND[h.kind] || h.kind, h.certified === false ? el("span", { class: "badge" }, tr("Draft")) : null),
            el("td", { class: "nowrap" }, h.table ? `${h.database}.${h.table}` : h.database),
            el("td", {}, el("button", { type: "button", class: "link", onclick: () => openHit(h) }, h.name)),
            el("td", { class: "muted" }, h.label || h.matched),
          ),
        ),
      ),
    ),
    ),
  );
}

async function openHit(hit) {
  $("catalog-q").value = "";
  review.searching = false;
  if (!review.list) await loadCatalogs();
  if (!hit.table) review.table = null; // an abbreviation lives on the overview
  await openCatalog(hit.database);
  if (hit.table) await openTable(hit.table);
}

$("catalog-q").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 250);
});

$("tab-catalog").addEventListener("click", () => {
  if (!review.list) loadCatalogs();
});
$("auth").addEventListener("submit", () => {
  review.list = null;
  if (!$("view-catalog").hidden) loadCatalogs();
});

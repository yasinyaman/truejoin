"use strict";
// The audit trail: who read or changed what, newest first, from GET /audit.
// Uses el, api and $ from console.js. Events carry names, never row values.

const AUDIT_CHOICES = {
  event: ["", "data_access", "catalog_change", "access_change", "tool_call", "sign_in"],
  action: ["", "read", "query", "export", "create", "update", "delete", "tool", "sign_in"],
};

// Which of the audit's two readings is on screen: the events, or the usage they add up to.
let auditMode = "events";

// The switch between the two readings, the events' filters across the top (set, then
// applied together), and the usage's period.
function auditForm() {
  const select = (name, label) =>
    el("label", {}, label, el("select", { name }, ...AUDIT_CHOICES[name].map((v) => el("option", { value: v }, v || tr("Any")))));
  const text = (name, label) => el("label", {}, label, el("input", { class: "field", name, placeholder: tr("Any") }));
  const mode = (value, label) => el("label", {}, el("input", { type: "radio", name: "audit-mode", value, checked: value === auditMode }), ` ${label}`);
  const modes = el("fieldset", { class: "modes" }, el("legend", { class: "sr-only" }, tr("Show")), mode("events", tr("Events")), mode("usage", tr("Usage")));
  modes.addEventListener("change", (e) => go("audit", e.target.value === "usage" ? "usage" : null));
  const form = el(
    "form",
    { class: "filter-bar", id: "audit-events-bar", "aria-label": tr("Audit filters") },
    select("event", tr("Event")),
    select("action", tr("Action")),
    text("actor", tr("Who (key name)")),
    text("database", tr("Database")),
    text("table", tr("Table")),
    text("sql_fingerprint", tr("Query fingerprint")),
    el("label", {}, tr("Show"), el("select", { name: "limit" }, ...["100", "250", "500"].map((v) => el("option", { value: v }, v)))),
    el("label", { class: "check" }, el("input", { type: "checkbox", name: "restricted" }), tr(" Only restricted reads")),
    el(
      "div",
      { class: "bar" },
      el("button", { type: "submit" }, tr("Apply")),
      el("span", { class: "muted", id: "audit-set" }),
      el(
        "button",
        {
          type: "button",
          class: "link",
          id: "audit-clear",
          hidden: true,
          onclick: () => {
            form.reset();
            loadAudit();
          },
        },
        tr("Clear"),
      ),
    ),
  );
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    loadAudit();
  });
  const days = el("select", { name: "days", "aria-label": tr("Usage period") }, ...[["", tr("all time")], ["7", tr("7 days")], ["30", tr("30 days")], ["90", tr("90 days")]].map(([v, t]) => el("option", { value: v }, t)));
  days.addEventListener("change", loadUsage);
  return [modes, form, el("div", { class: "filter-bar", id: "audit-usage-bar", hidden: true }, el("label", {}, tr("Usage over"), days))];
}

function auditQuery() {
  const params = new URLSearchParams();
  for (const [name, value] of new FormData($("audit-events-bar"))) {
    if (name === "restricted") params.set(name, "true");
    else if (String(value).trim()) params.set(name, String(value).trim());
  }
  // What is set is said beside Apply, with a way back to everything.
  const set = [...params.keys()].filter((name) => name !== "limit").length;
  $("audit-set").textContent = set ? tr`${set} filter(s)` : "";
  $("audit-clear").hidden = !set;
  return params.toString();
}

// The reading the address names: #admin/audit, or #admin/audit/usage.
function openAudit(mode) {
  auditMode = mode === "usage" ? "usage" : "events";
  for (const radio of $("audit-filters").querySelectorAll('input[name="audit-mode"]')) radio.checked = radio.value === auditMode;
  $("audit-events-bar").hidden = auditMode !== "events";
  $("audit-usage-bar").hidden = auditMode !== "usage";
  return auditMode === "usage" ? loadUsage() : loadAudit();
}

function who(e) {
  const extra = [e.tenant ? tr`tenant ${e.tenant}` : "", e.roles.length ? e.roles.join(", ") : ""].filter(Boolean).join(" · ");
  return el("td", {}, e.actor || "anonymous", extra ? el("div", { class: "muted" }, extra) : null);
}

function withheld(e) {
  const parts = [];
  if (e.row_filtered) parts.push(tr("rows filtered"));
  if ((e.masked_columns || []).length) parts.push(tr`masked: ${e.masked_columns.join(", ")}`);
  if ((e.filtered_columns || []).length) parts.push(tr`filtered on: ${e.filtered_columns.join(", ")}`);
  return el("td", {}, parts.join("; "));
}

const OUTCOME_CLASS = { answered: "num", empty: "num warn", refused: "num warn", failed: "num error", error: "num error" };

// A tool call: what was asked, how it was read (names only), and how it went.
function callRow(e) {
  const read = [
    e.measures.length ? e.measures.join(", ") : "",
    e.dimensions.length ? tr`by ${e.dimensions.join(", ")}` : "",
    e.glossary.length ? tr`(${e.glossary.join(", ")} written out)` : "",
  ].filter(Boolean).join(" ");
  return el(
    "tr",
    {},
    el("td", { class: "nowrap", title: e.at }, new Date(e.at).toLocaleString()),
    who(e),
    el("td", {}, el("span", { class: "badge" }, tr("Tool")), ` ${e.tool}`, e.question ? el("div", { class: "muted" }, `“${e.question}”`) : null, read ? el("div", { class: "muted" }, read) : null),
    el("td", {}, e.table),
    el("td", { class: OUTCOME_CLASS[e.outcome] || "num" }, e.outcome),
    el("td", { class: "num" }, e.row_count ?? ""),
    withheld(e),
    el("td", { class: "muted", title: e.request_id }, (e.request_id || "").slice(0, 8)),
  );
}

// A sign-in: how, by whom, from where, and whether it was let in. Never the secret.
function signInRow(e) {
  const refused = e.outcome !== "signed_in";
  return el(
    "tr",
    {},
    el("td", { class: "nowrap", title: e.at }, new Date(e.at).toLocaleString()),
    el("td", {}, e.actor || "anonymous", e.client ? el("div", { class: "muted" }, tr`from ${e.client}`) : null),
    el("td", {}, el("span", { class: "badge" }, tr("Sign-in")), ` ${e.method}`, e.reason ? el("div", { class: "muted" }, e.reason) : null),
    el("td", {}, ""),
    el("td", { class: refused ? "num warn" : "num" }, e.outcome),
    el("td", { class: "num" }, ""),
    el("td", {}, ""),
    el("td", { class: "muted", title: e.request_id }, (e.request_id || "").slice(0, 8)),
  );
}

// A query's fingerprint: the same statement asked with other values has the same one.
function sameQuery(e) {
  if (!e.sql_fingerprint) return null;
  const find = () => {
    $("audit-events-bar").elements.sql_fingerprint.value = e.sql_fingerprint;
    loadAudit();
  };
  return el(
    "div",
    { class: "muted" },
    el("code", { title: e.sql_fingerprint }, e.sql_fingerprint.slice(0, 12)),
    " ",
    el("button", { type: "button", class: "link", onclick: find }, tr("Same query")),
  );
}

function auditRow(e) {
  if (e.event === "tool_call") return callRow(e);
  if (e.event === "sign_in") return signInRow(e);
  const failed = e.status >= 400;
  return el(
    "tr",
    {},
    el("td", { class: "nowrap", title: e.at }, new Date(e.at).toLocaleString()),
    who(e),
    el("td", {}, e.event === "catalog_change" || e.event === "access_change" ? el("span", { class: "badge" }, e.event === "catalog_change" ? tr("Catalog") : tr("Access")) : null, " ", e.change || e.action, sameQuery(e)),
    el("td", {}, e.table ? `${e.database}.${e.table}` : e.database),
    el("td", { class: failed ? "num error" : "num" }, String(e.status)),
    el("td", { class: "num" }, e.row_count ?? ""),
    withheld(e),
    el("td", { class: "muted", title: e.request_id }, e.request_id.slice(0, 8)),
  );
}

async function loadAudit() {
  $("audit-status").className = "muted";
  $("audit-status").textContent = tr("Loading…");
  try {
    const data = await api("GET", `${API_PREFIX}/audit?${auditQuery()}`);
    if (!data.available) {
      $("audit-status").textContent = data.detail;
      $("audit-body").replaceChildren();
      return;
    }
    $("audit-status").textContent =
      tr`${data.events.length} event(s), newest first.` + (data.complete ? "" : tr(" Only the newest part of the audit file was searched."));
    const head = [tr("When"), tr("Who"), tr("What"), tr("Where"), tr("Status"), tr("Rows"), tr("Withheld"), tr("Request")];
    $("audit-body").replaceChildren(
      data.events.length
        ? el(
            "div",
            { class: "scroll" },
            el(
              "table",
              { class: "review" },
              el("thead", {}, el("tr", {}, ...head.map((h) => el("th", { scope: "col" }, h)))),
              el("tbody", {}, ...data.events.map(auditRow)),
            ),
          )
        : el("p", { class: "muted" }, tr("No event matches.")),
    );
  } catch (e) {
    $("audit-status").className = "error";
    $("audit-status").textContent = e.status === 403
      ? tr("This key cannot read the audit trail: that needs the audit permission.")
      : e.message;
    $("audit-body").replaceChildren();
  }
}

// What people asked and how it went, from the newest 500 tool calls.
function tallyList(title, entries, none) {
  return el(
    "section",
    { class: "term" },
    el("h4", {}, title),
    entries.length
      ? el("ul", { class: "plain" }, ...entries.slice(0, 10).map(([name, n]) => el("li", {}, el("code", {}, name), el("span", { class: "muted" }, ` ${n}`))))
      : el("p", { class: "muted" }, none),
  );
}

// Where a gap goes: an approved catalog's glossary (written out as other
// words) or its certified terms (the word itself, as a term's or a column's
// synonym). Read with it from then on; the routes check the key.
// ``after`` runs once a word is added; ``status`` is where a failure is said.
function writeOut(word, catalogs, after = loadUsage, status = "audit-status") {
  const draft = (t) => (t.certified ? "" : tr(" (draft: read once certified)"));
  const targets = catalogs.flatMap(({ db, terms }) => [
    { label: tr`Glossary of ${db}: means…`, db, kind: "glossary" },
    ...terms.flatMap((t) => [
      { label: tr`${db}: a name for ${t.name}` + draft(t), db, kind: "term", term: t.name },
      ...t.columns.map((c) => ({ label: tr`${db}: a word for ${t.name} › ${c}` + draft(t), db, kind: "column", term: t.name, column: c })),
    ]),
  ]);
  const input = el("input", { class: "field", "aria-label": tr`${word} means`, placeholder: tr("words it means"), maxlength: "200" });
  const pick = el("select", { "aria-label": tr`Where ${word} goes` }, ...targets.map((t, i) => el("option", { value: String(i) }, t.label)));
  pick.addEventListener("change", () => (input.hidden = targets[pick.value].kind !== "glossary"));
  // The input keeps its cell when hidden, so rows laid out as a grid stay aligned.
  const form = el("form", {}, pick, el("span", { class: "means" }, input), el("button", { type: "submit", class: "secondary" }, tr("Add")));
  // Saves the word where the form points; false when there is nothing to save yet.
  form.add = async () => {
    const t = targets[pick.value];
    const long = input.value.trim();
    if (t.kind === "glossary" && !long) {
      input.focus();
      return false;
    }
    const [path, body] =
      t.kind === "glossary"
        ? ["guidance", { add_to_glossary: { [word]: long } }]
        : [`terms/${encodeURIComponent(t.term)}`, t.kind === "term" ? { add_synonyms: [word] } : { add_to_columns: { [t.column]: [word] } }];
    try {
      await api("PATCH", `${API_PREFIX}/catalog/${encodeURIComponent(t.db)}/${path}`, body);
      return t.label;
    } catch (err) {
      $(status).className = "error";
      $(status).textContent = err.message;
      return false;
    }
  };
  // Points the form at a place someone proposed; false when this key cannot write there.
  form.propose = ({ database, term, column }) => {
    const at = targets.findIndex((t) => t.db === database && t.term === term && (column ? t.column === column : t.kind === "term"));
    if (at < 0) return false;
    pick.value = String(at);
    pick.dispatchEvent(new Event("change"));
    return true;
  };
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const added = await form.add();
    form.dispatchEvent(new CustomEvent("word-added", { detail: added }));
    if (added) await after(); // counted (or asked) again: a word now read is read
  });
  return form;
}

// Words people asked with that the certified vocabulary does not know, the
// ones in unanswered questions first: what a steward should add next.
function vocabularyGaps(v, catalogs = []) {
  if (!v) return [];
  if (v.available === false) return [el("h4", {}, tr("Words the vocabulary does not know")), el("p", { class: "muted" }, v.detail)];
  const c = v.covered;
  return [
    el("h4", {}, tr("Words the vocabulary does not know")),
    el("p", { class: "muted" }, tr`Over ${v.questions} distinct question(s): ${c.all} call(s) fully covered, ${c.part} in part, ${c.none} naming nothing it knows. ${catalogs.length ? tr("The Means column puts each word where it belongs: the glossary, a term, or one of its columns.") : tr("Add the words below as synonyms or values, or to the glossary.")}`),
    v.gaps.length
      ? el(
          "div",
          { class: "scroll" },
          el(
            "table",
            { class: "review" },
            el("thead", {}, el("tr", {}, ...[tr("Word"), tr("Questions"), tr("Asked"), tr("Not answered"), tr("For example"), ...(catalogs.length ? [tr("Means")] : [])].map((h) => el("th", { scope: "col" }, h)))),
            el(
              "tbody",
              {},
              ...v.gaps.map((g) => el("tr", {}, el("td", {}, el("code", {}, g.word)), el("td", {}, String(g.questions)), el("td", {}, String(g.asked)), el("td", {}, String(g.unanswered)), el("td", {}, g.example), catalogs.length ? el("td", {}, writeOut(g.word, catalogs)) : null)),
            ),
          ),
        )
      : el("p", { class: "muted" }, tr("Every word asked with is in the vocabulary.")),
  ];
}

// Every catalog this key may read, with its terms: which are certified, and
// the columns each has words for. Empty when it may read none.
// shortcut: reads each catalog whole for the names of its terms; give it a
// terms-only route when a catalog grows to hundreds of tables.
async function vocabulary() {
  try {
    const listed = await api("GET", `${API_PREFIX}/catalog`);
    return await Promise.all(
      listed.catalogs.map(async (c) => {
        const o = await api("GET", `${API_PREFIX}/catalog/${encodeURIComponent(c.database_name)}/ontology`);
        const terms = Object.values(o.terms || {}).map((t) => ({ name: t.name, certified: Boolean(t.certified), columns: t.column_attributes.map((a) => a.column) }));
        return { db: c.database_name, status: c.status, languages: c.languages || [], terms };
      }),
    );
  } catch {
    return [];
  }
}

// The approved catalogs this key may add words to, with their terms and
// columns (a draft term takes words too, and reads them once certified);
// none when it may not edit catalogs.
async function glossaries(known) {
  try {
    if (!ACCESS.update) return [];
    return (known || (await vocabulary())).filter((c) => c.status === "approved");
  } catch {
    return [];
  }
}

async function loadUsage() {
  $("audit-status").className = "muted";
  $("audit-status").textContent = tr("Counting…");
  try {
    const days = $("audit-filters").querySelector('select[name="days"]').value;
    const data = await api("GET", `${API_PREFIX}/audit/usage${days ? `?days=${days}` : ""}`);
    if (!data.available) {
      $("audit-status").textContent = data.detail;
      return $("audit-body").replaceChildren();
    }
    const o = data.outcomes;
    const catalogs = data.vocabulary?.gaps?.length ? await glossaries() : [];
    const span = data.from ? tr` from ${new Date(data.from).toLocaleString()} to ${new Date(data.to).toLocaleString()}` : "";
    $("audit-status").textContent =
      tr`${data.calls} call(s)${days ? tr` in the last ${days} days` : ""}${span}: ${o.answered} answered, ${o.empty} with no rows, ${o.refused} refused, ${o.failed + o.error} failed.`;
    const unanswered = data.unanswered;
    $("audit-body").replaceChildren(
      el(
        "div",
        { class: "usage" },
        tallyList(tr("Tools"), data.tools, tr("No calls yet.")),
        tallyList(tr("Tables"), data.tables, tr("No table named.")),
        tallyList(tr("Measures"), data.measures, tr("No measure asked for.")),
        tallyList(tr("Breakdowns"), data.dimensions, tr("No breakdown asked for.")),
        tallyList(tr("Filtered on"), data.filtered_columns, tr("No condition.")),
        tallyList(tr("Abbreviations written out"), data.glossary, tr("None used.")),
      ),
      ...vocabularyGaps(data.vocabulary, catalogs),
      el("h4", {}, tr("Not answered")),
      el("p", { class: "muted" }, data.questions_kept
        ? tr("Where the vocabulary falls short: the newest calls that met no rows, a refusal or an error.")
        : tr("The words of questions are not kept (audit.question_text is off), so only the reading shows.")),
      unanswered.length
        ? el(
            "div",
            { class: "scroll" },
            el(
              "table",
              { class: "review" },
              el("thead", {}, el("tr", {}, ...[tr("When"), tr("Who"), tr("What"), tr("Where"), tr("Outcome"), tr("Rows"), tr("Filtered"), tr("Request")].map((h) => el("th", { scope: "col" }, h)))),
              el("tbody", {}, ...unanswered.map(callRow)),
            ),
          )
        : el("p", { class: "muted" }, tr("Every call was answered.")),
    );
  } catch (e) {
    $("audit-status").className = "error";
    $("audit-status").textContent = e.status === 403
      ? tr("This key cannot read the audit trail: that needs the audit permission.")
      : e.message;
    $("audit-body").replaceChildren();
  }
}

$("audit-filters").replaceChildren(...auditForm());
// #admin/audit, #admin/audit/usage
page("audit", { needs: "audit", area: "admin", open: ([mode]) => openAudit(mode) });

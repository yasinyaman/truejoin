"use strict";
// The audit trail: who read or changed what, newest first, from GET /audit.
// Uses el, api and $ from console.js. Events carry names, never row values.

const AUDIT_CHOICES = {
  event: ["", "data_access", "catalog_change", "access_change", "tool_call"],
  action: ["", "read", "query", "export", "create", "update", "delete", "tool"],
};

function auditForm() {
  const select = (name, label) =>
    el("label", {}, label, el("select", { name }, ...AUDIT_CHOICES[name].map((v) => el("option", { value: v }, v || "any"))));
  const text = (name, label) => el("label", {}, label, el("input", { class: "field", name, placeholder: "any" }));
  const form = el(
    "form",
    { class: "filters", "aria-label": "Audit filters" },
    el("h3", {}, "Filter"),
    select("event", "Event"),
    select("action", "Action"),
    text("actor", "Who (key name)"),
    text("database", "Database"),
    text("table", "Table"),
    el("label", { class: "check" }, el("input", { type: "checkbox", name: "restricted" }), " Only restricted reads"),
    el("label", {}, "Show", el("select", { name: "limit" }, ...["100", "250", "500"].map((v) => el("option", { value: v }, v)))),
    el("button", { type: "submit" }, "Show events"),
    el("label", {}, "Usage over", el("select", { name: "days", "aria-label": "Usage period" }, ...[["", "all time"], ["7", "7 days"], ["30", "30 days"], ["90", "90 days"]].map(([v, t]) => el("option", { value: v }, t)))),
    el("button", { type: "button", class: "secondary", onclick: loadUsage }, "Usage summary"),
  );
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    loadAudit();
  });
  return form;
}

function auditQuery() {
  const params = new URLSearchParams();
  for (const [name, value] of new FormData($("audit-filters").querySelector("form"))) {
    if (name === "days") continue; // the usage summary's, not a filter
    if (name === "restricted") params.set(name, "true");
    else if (String(value).trim()) params.set(name, String(value).trim());
  }
  return params.toString();
}

function who(e) {
  const extra = [e.tenant ? `tenant ${e.tenant}` : "", e.roles.length ? e.roles.join(", ") : ""].filter(Boolean).join(" · ");
  return el("td", {}, e.actor || "anonymous", extra ? el("div", { class: "muted" }, extra) : null);
}

function withheld(e) {
  const parts = [];
  if (e.row_filtered) parts.push("rows filtered");
  if ((e.masked_columns || []).length) parts.push(`masked: ${e.masked_columns.join(", ")}`);
  if ((e.filtered_columns || []).length) parts.push(`filtered on: ${e.filtered_columns.join(", ")}`);
  return el("td", {}, parts.join("; "));
}

const OUTCOME_CLASS = { answered: "num", empty: "num warn", refused: "num warn", failed: "num error", error: "num error" };

// A tool call: what was asked, how it was read (names only), and how it went.
function callRow(e) {
  const read = [
    e.measures.length ? e.measures.join(", ") : "",
    e.dimensions.length ? `by ${e.dimensions.join(", ")}` : "",
    e.glossary.length ? `(${e.glossary.join(", ")} written out)` : "",
  ].filter(Boolean).join(" ");
  return el(
    "tr",
    {},
    el("td", { class: "nowrap", title: e.at }, new Date(e.at).toLocaleString()),
    who(e),
    el("td", {}, el("span", { class: "badge" }, "tool"), ` ${e.tool}`, e.question ? el("div", { class: "muted" }, `“${e.question}”`) : null, read ? el("div", { class: "muted" }, read) : null),
    el("td", {}, e.table),
    el("td", { class: OUTCOME_CLASS[e.outcome] || "num" }, e.outcome),
    el("td", { class: "num" }, e.row_count ?? ""),
    withheld(e),
    el("td", { class: "muted", title: e.request_id }, (e.request_id || "").slice(0, 8)),
  );
}

function auditRow(e) {
  if (e.event === "tool_call") return callRow(e);
  const failed = e.status >= 400;
  return el(
    "tr",
    {},
    el("td", { class: "nowrap", title: e.at }, new Date(e.at).toLocaleString()),
    who(e),
    el("td", {}, e.event === "catalog_change" || e.event === "access_change" ? el("span", { class: "badge" }, e.event === "catalog_change" ? "catalog" : "access") : null, " ", e.change || e.action),
    el("td", {}, e.table ? `${e.database}.${e.table}` : e.database),
    el("td", { class: failed ? "num error" : "num" }, String(e.status)),
    el("td", { class: "num" }, e.row_count ?? ""),
    withheld(e),
    el("td", { class: "muted", title: e.request_id }, e.request_id.slice(0, 8)),
  );
}

async function loadAudit() {
  $("audit-status").className = "muted";
  $("audit-status").textContent = "Loading…";
  try {
    const data = await api("GET", `${API_PREFIX}/audit?${auditQuery()}`);
    if (!data.available) {
      $("audit-status").textContent = data.detail;
      $("audit-body").replaceChildren();
      return;
    }
    $("audit-status").textContent =
      `${data.events.length} event(s), newest first.` + (data.complete ? "" : " Only the newest part of the audit file was searched.");
    const head = ["When", "Who", "What", "Where", "Status", "Rows", "Withheld", "Request"];
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
        : el("p", { class: "muted" }, "No event matches."),
    );
  } catch (e) {
    $("audit-status").className = "error";
    $("audit-status").textContent = e.message.startsWith("403")
      ? "This key cannot read the audit trail: that needs the audit permission."
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

// Words people asked with that the certified vocabulary does not know, the
// ones in unanswered questions first: what a steward should add next.
function vocabularyGaps(v) {
  if (!v) return [];
  if (v.available === false) return [el("h4", {}, "Words the vocabulary does not know"), el("p", { class: "muted" }, v.detail)];
  const c = v.covered;
  return [
    el("h4", {}, "Words the vocabulary does not know"),
    el("p", { class: "muted" }, `Over ${v.questions} distinct question(s): ${c.all} call(s) fully covered, ${c.part} in part, ${c.none} naming nothing it knows. Add the words below as synonyms or values, or to the glossary.`),
    v.gaps.length
      ? el(
          "div",
          { class: "scroll" },
          el(
            "table",
            { class: "review" },
            el("thead", {}, el("tr", {}, ...["Word", "Questions", "Asked", "Not answered", "For example"].map((h) => el("th", { scope: "col" }, h)))),
            el(
              "tbody",
              {},
              ...v.gaps.map((g) => el("tr", {}, el("td", {}, el("code", {}, g.word)), el("td", {}, String(g.questions)), el("td", {}, String(g.asked)), el("td", {}, String(g.unanswered)), el("td", {}, g.example))),
            ),
          ),
        )
      : el("p", { class: "muted" }, "Every word asked with is in the vocabulary."),
  ];
}

async function loadUsage() {
  $("audit-status").className = "muted";
  $("audit-status").textContent = "Counting…";
  try {
    const days = $("audit-filters").querySelector('select[name="days"]').value;
    const data = await api("GET", `${API_PREFIX}/audit/usage${days ? `?days=${days}` : ""}`);
    if (!data.available) {
      $("audit-status").textContent = data.detail;
      return $("audit-body").replaceChildren();
    }
    const o = data.outcomes;
    const span = data.from ? ` from ${new Date(data.from).toLocaleString()} to ${new Date(data.to).toLocaleString()}` : "";
    $("audit-status").textContent =
      `${data.calls} call(s)${days ? ` in the last ${days} days` : ""}${span}: ${o.answered} answered, ${o.empty} with no rows, ${o.refused} refused, ${o.failed + o.error} failed.`;
    const unanswered = data.unanswered;
    $("audit-body").replaceChildren(
      el(
        "div",
        { class: "usage" },
        tallyList("Tools", data.tools, "No calls yet."),
        tallyList("Tables", data.tables, "No table named."),
        tallyList("Measures", data.measures, "No measure asked for."),
        tallyList("Breakdowns", data.dimensions, "No breakdown asked for."),
        tallyList("Filtered on", data.filtered_columns, "No condition."),
        tallyList("Abbreviations written out", data.glossary, "None used."),
      ),
      ...vocabularyGaps(data.vocabulary),
      el("h4", {}, "Not answered"),
      el("p", { class: "muted" }, data.questions_kept
        ? "Where the vocabulary falls short: the newest calls that met no rows, a refusal or an error."
        : "The words of questions are not kept (audit.question_text is off), so only the reading shows."),
      unanswered.length
        ? el(
            "div",
            { class: "scroll" },
            el(
              "table",
              { class: "review" },
              el("thead", {}, el("tr", {}, ...["When", "Who", "What", "Where", "Outcome", "Rows", "Filtered", "Request"].map((h) => el("th", { scope: "col" }, h)))),
              el("tbody", {}, ...unanswered.map(callRow)),
            ),
          )
        : el("p", { class: "muted" }, "Every call was answered."),
    );
  } catch (e) {
    $("audit-status").className = "error";
    $("audit-status").textContent = e.message.startsWith("403")
      ? "This key cannot read the audit trail: that needs the audit permission."
      : e.message;
    $("audit-body").replaceChildren();
  }
}

$("audit-filters").replaceChildren(auditForm());
$("tab-audit").addEventListener("click", loadAudit);
$("auth").addEventListener("submit", () => {
  if (!$("view-audit").hidden) loadAudit();
});

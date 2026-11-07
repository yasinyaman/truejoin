"use strict";
// The audit trail: who read or changed what, newest first, from GET /audit.
// Uses el, api and $ from console.js. Events carry names, never row values.

const AUDIT_CHOICES = {
  event: ["", "data_access", "catalog_change", "tool_call"],
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
    el("td", {}, e.event === "catalog_change" ? el("span", { class: "badge" }, "catalog") : null, " ", e.change || e.action),
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
function tally(items) {
  const counts = new Map();
  for (const item of items) if (item) counts.set(item, (counts.get(item) || 0) + 1);
  return [...counts.entries()].sort((a, b) => b[1] - a[1]);
}

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

async function loadUsage() {
  $("audit-status").className = "muted";
  $("audit-status").textContent = "Loading…";
  try {
    const data = await api("GET", `${API_PREFIX}/audit?event=tool_call&limit=500`);
    if (!data.available) {
      $("audit-status").textContent = data.detail;
      return $("audit-body").replaceChildren();
    }
    const calls = data.events;
    const by = (outcome) => calls.filter((c) => c.outcome === outcome).length;
    $("audit-status").textContent =
      `${calls.length} call(s): ${by("answered")} answered, ${by("empty")} with no rows, ${by("refused")} refused, ${by("failed") + by("error")} failed.` +
      (data.complete ? "" : " Only the newest part of the audit file was searched.");
    const unanswered = calls.filter((c) => c.outcome !== "answered");
    $("audit-body").replaceChildren(
      el(
        "div",
        { class: "usage" },
        tallyList("Tools", tally(calls.map((c) => c.tool)), "No calls yet."),
        tallyList("Tables", tally(calls.map((c) => c.table)), "No table named."),
        tallyList("Measures", tally(calls.flatMap((c) => c.measures)), "No measure asked for."),
        tallyList("Breakdowns", tally(calls.flatMap((c) => c.dimensions)), "No breakdown asked for."),
        tallyList("Filtered on", tally(calls.flatMap((c) => c.filtered_columns)), "No condition."),
        tallyList("Abbreviations written out", tally(calls.flatMap((c) => c.glossary)), "None used."),
      ),
      el("h4", {}, "Not answered"),
      el("p", { class: "muted" }, calls.some((c) => c.question)
        ? "Where the vocabulary falls short: questions that met no rows, a refusal or an error."
        : "The words of questions are not kept (audit.question_text is off), so only the reading shows."),
      unanswered.length
        ? el(
            "div",
            { class: "scroll" },
            el(
              "table",
              { class: "review" },
              el("thead", {}, el("tr", {}, ...["When", "Who", "What", "Where", "Outcome", "Rows", "Filtered", "Request"].map((h) => el("th", { scope: "col" }, h)))),
              el("tbody", {}, ...unanswered.slice(0, 100).map(callRow)),
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

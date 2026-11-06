"use strict";
// The audit trail: who read or changed what, newest first, from GET /audit.
// Uses el, api and $ from console.js. Events carry names, never row values.

const AUDIT_CHOICES = {
  event: ["", "data_access", "catalog_change"],
  action: ["", "read", "query", "export", "create", "update", "delete"],
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
  if (e.masked_columns.length) parts.push(`masked: ${e.masked_columns.join(", ")}`);
  if (e.filtered_columns.length) parts.push(`filtered on: ${e.filtered_columns.join(", ")}`);
  return el("td", {}, parts.join("; "));
}

function auditRow(e) {
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

$("audit-filters").replaceChildren(auditForm());
$("tab-audit").addEventListener("click", loadAudit);
$("auth").addEventListener("submit", () => {
  if (!$("view-audit").hidden) loadAudit();
});

"use strict";
// API keys: hand one out, see who holds what, revoke. From /keys, which needs
// the admin permission. Uses el, api and $ from console.js. A new key's secret
// is shown once, in this page, and kept by the server only as a digest.

const KEY_PERMISSIONS = ["read", "query", "create", "update", "delete", "audit", "admin", "all"];

function keyForm() {
  const perms = el(
    "fieldset",
    { class: "modes wrap" },
    el("legend", { class: "sr-only" }, "Permissions"),
    ...KEY_PERMISSIONS.map((p) => el("label", {}, el("input", { type: "checkbox", name: "permissions", value: p, checked: p === "read" }), ` ${p}`)),
  );
  const form = el(
    "form",
    { class: "filters", "aria-label": "Hand out a key" },
    el("h3", {}, "Hand out a key"),
    el("label", {}, "Name (who or what holds it)", el("input", { class: "field", name: "name", required: true, maxlength: 64, placeholder: "etl-nightly" })),
    el("label", {}, "Permissions", perms),
    el("label", {}, "Tenant", el("input", { class: "field", name: "tenant", placeholder: "none" })),
    el("label", {}, "Expires after (days)", el("input", { class: "field", name: "expires_in_days", type: "number", min: 1, max: 3650, placeholder: "never" })),
    el(
      "label",
      {},
      "Row rules (JSON, as in the config)",
      el("textarea", { name: "row_filters", rows: 3, spellcheck: "false", placeholder: '{"orders": [{"column": "tenant", "value": "${tenant}"}]}' }),
    ),
    el("button", { type: "submit" }, "Hand out"),
    el("p", { class: "muted" }, "You can give only what your own key holds, and a new key carries your roles. A key cannot be edited: revoke it and hand out another."),
  );
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    createKey(form);
  });
  return form;
}

async function createKey(form) {
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
  if (rules) {
    try {
      body.row_filters = JSON.parse(rules);
    } catch (e) {
      return keysSay(`The row rules are not JSON: ${e.message}`, true);
    }
  }
  try {
    const made = await api("POST", `${API_PREFIX}/keys`, body);
    form.reset();
    showSecret(made.key.name, made.secret);
    await loadKeys(false);
  } catch (e) {
    keysSay(e.message, true);
  }
}

// The one time the secret exists outside its holder's hands.
function showSecret(name, secret) {
  const value = el("input", { class: "field", readonly: true, value: secret, "aria-label": `The key for ${name}` });
  const copy = el("button", { type: "button", class: "secondary" }, "Copy");
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(secret);
      copy.textContent = "Copied";
    } catch {
      value.select();
    }
  });
  const box = el(
    "div",
    { class: "term", role: "alert" },
    el("p", {}, el("strong", {}, `The key for ${name}`), " — shown this once. Copy it now; it cannot be shown again."),
    el("div", { class: "bar" }, value, copy, el("button", { type: "button", class: "link", onclick: () => box.remove() }, "Done")),
  );
  $("keys-secret").replaceChildren(box);
  value.select();
}

function keysSay(text, error = false) {
  $("keys-status").className = error ? "error" : "muted";
  $("keys-status").textContent = text;
}

function keyState(k) {
  if (k.revoked_at) return el("span", { class: "badge" }, "revoked");
  if (!k.active) return el("span", { class: "badge status-modified" }, "expired");
  return el("span", { class: "badge status-approved" }, "active");
}

function day(iso) {
  return iso ? iso.slice(0, 10) : "";
}

function grantCells(k) {
  const rules = Object.entries(k.row_filters || {});
  return [
    el("td", {}, k.permissions.join(", ")),
    el("td", {}, [k.tenant ? `tenant ${k.tenant}` : "", (k.roles || []).join(", ")].filter(Boolean).join(" · ")),
    el("td", {}, rules.length ? rules.map(([table, list]) => `${table}: ${list.map((r) => r.column).join(", ")}`).join("; ") : ""),
  ];
}

function keyRow(k) {
  const revoke = el("button", { type: "button", class: "secondary" }, "Revoke");
  revoke.addEventListener("click", async () => {
    if (!confirm(`Revoke ${k.name}? Whoever holds it is refused from the next request on. This cannot be undone.`)) return;
    try {
      await api("DELETE", `${API_PREFIX}/keys/${encodeURIComponent(k.id)}`);
      keysSay(`${k.name} is revoked.`);
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
    el("td", {}, k.revoked_at ? `revoked ${day(k.revoked_at)}` : k.expires_at ? day(k.expires_at) : "never"),
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

async function loadKeys(clearStatus = true) {
  if (clearStatus) keysSay("Loading…");
  try {
    const data = await api("GET", `${API_PREFIX}/keys`);
    $("keys-filters").hidden = !data.available;
    const handed = data.keys.length
      ? keysTable(["Name", "State", "Permissions", "Tenant · roles", "Row rules on", "Made", "Expires", ""], data.keys.map(keyRow))
      : el("p", { class: "muted" }, "No key handed out yet.");
    const configured = keysTable(
      ["Name", "Permissions", "Tenant · roles", "Row rules"],
      data.configured.map((k) =>
        el(
          "tr",
          {},
          el("td", {}, k.name),
          el("td", {}, k.permissions.join(", ")),
          el("td", {}, [k.tenant ? `tenant ${k.tenant}` : "", (k.roles || []).join(", ")].filter(Boolean).join(" · ")),
          el("td", {}, k.row_rules ? "yes" : ""),
        ),
      ),
    );
    $("keys-body").replaceChildren(
      el("h3", {}, "Handed out here"),
      data.available ? handed : el("p", { class: "muted" }, data.detail),
      el("h3", {}, "From the configuration"),
      el("p", { class: "muted" }, "Changed in the configuration file, not here."),
      configured,
    );
    if (clearStatus) keysSay(data.auth_enabled === false ? "Authentication is off: no key is checked until it is turned on." : "");
  } catch (e) {
    $("keys-filters").hidden = true;
    $("keys-body").replaceChildren();
    keysSay(e.message.startsWith("403") ? "This key cannot manage keys: that needs the admin permission." : e.message, true);
  }
}

$("keys-filters").replaceChildren(keyForm());
$("tab-keys").addEventListener("click", () => loadKeys());
$("auth").addEventListener("submit", () => {
  if (!$("view-keys").hidden) loadKeys();
});

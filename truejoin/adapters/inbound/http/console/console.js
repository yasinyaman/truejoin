"use strict";
// Every value from the server is placed with text nodes, never as HTML.

// Shared with catalog.js, which loads after this file.
const KEY_HEADER = document.querySelector('meta[name="truejoin-key-header"]').content;
const API_PREFIX = document.querySelector('meta[name="truejoin-api-prefix"]').content;
const STORE = "truejoin-console-key";
const $ = (id) => document.getElementById(id);
let mode = "sql";
// The SQL placeholder names a real table once the sources are known.
let exampleSql = "SELECT * FROM source.table LIMIT 100";
// The reading of the last answered question: what a follow-up changes.
let lastRequest = null;
let results = 0;

function el(tag, attrs = {}, ...children) {
  // Buttons and badges are Carbon components (carbon.js); everything else is plain HTML.
  let e;
  let skip = [];
  if (carbonable(tag, attrs)) [e, attrs, children, skip] = carbonEl(tag, attrs, children);
  else e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false || skip.includes(k)) continue;
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

// -- keeping a sign-in alive ---------------------------------------------
// A sign-in that came with a refresh token is renewed a minute before its
// access token expires, by this server (as the code was exchanged). Only the
// token this sign-in gave is renewed: a pasted key or token is left alone.
const SESSION = "truejoin-console-session";
let refreshing = null;
let refreshTimer = null;

function session() {
  try {
    return JSON.parse(sessionStorage.getItem(SESSION) || "null");
  } catch {
    return null;
  }
}

function keepSession(data) {
  try {
    if (data && data.refresh_token) {
      const { access_token, refresh_token, expires_at } = data;
      sessionStorage.setItem(SESSION, JSON.stringify({ access_token, refresh_token, expires_at }));
    } else sessionStorage.removeItem(SESSION);
  } catch {}
  scheduleRefresh();
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  const held = session();
  if (!held) return;
  const wait = held.expires_at * 1000 - Date.now() - 60000;
  refreshTimer = setTimeout(freshToken, Math.min(Math.max(wait, 0), 2 ** 31 - 1));
}

// Awaited before every call; one renewal at a time, since the provider may rotate the refresh token.
async function freshToken() {
  const held = session();
  if (!held || $("key").value.trim() !== held.access_token) return;
  if (held.expires_at * 1000 - Date.now() > 60000) return scheduleRefresh();
  refreshing ??= (async () => {
    try {
      const response = await fetch("/ui/sign-in/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: held.refresh_token }),
      });
      const data = await response.json().catch(() => ({}));
      if (response.status === 401) return keepSession(null); // expired or revoked: the next call says to sign in
      if (!response.ok) return retrySoon(held);
      $("key").value = data.access_token;
      remember(data.access_token);
      keepSession(data);
    } catch {
      retrySoon(held);
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

// The provider (or this server) did not answer: try again shortly, while the token still holds.
function retrySoon(held) {
  clearTimeout(refreshTimer);
  if (held.expires_at * 1000 > Date.now()) refreshTimer = setTimeout(freshToken, 10000);
}

// A tab put to sleep misses its timer; check again when it is looked at.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") freshToken();
});

// What says who is calling: an API key in its header, or a token from the
// identity provider (three dot-separated parts) as a bearer token.
function withAuth(headers = {}) {
  const key = $("key").value.trim();
  if (/^[\w-]+\.[\w-]+\.[\w-]+$/.test(key)) headers.Authorization = `Bearer ${key}`;
  else if (key) headers[KEY_HEADER] = key;
  return headers;
}

async function api(method, path, body) {
  await freshToken();
  const headers = withAuth();
  const key = $("key").value.trim();
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
    // The detail is what a person reads; the status is kept for the code that asks.
    const error = new Error(text || response.statusText || tr("The request failed"));
    error.status = response.status;
    throw error;
  }
  remember(key);
  return data;
}

function rowsLabel(table) {
  const n = table.row_count ?? table.row_estimate;
  // A source that cannot estimate reports -1.
  return n === null || n === undefined || n < 0 ? "" :tr`${table.row_count == null ? "~" : ""}${n.toLocaleString()} rows`;
}

async function loadSources() {
  const nav = $("tables");
  nav.replaceChildren(el("p", { class: "muted" }, tr("Loading…")));
  try {
    const { sources } = await api("GET", "/olap/sources");
    nav.replaceChildren(
      ...(sources.length ? sources.map(sourceNode) : [el("p", { class: "muted" }, tr("No sources connected."))]),
    );
    const first = sources.find((s) => s.tables.length)?.tables[0];
    if (first) exampleSql = `SELECT * FROM ${first.name} LIMIT 100`;
    if (mode === "sql") $("input").placeholder = exampleSql;
  } catch (e) {
    nav.replaceChildren(el("p", { class: "error" }, e.message));
  }
}

function sourceNode(source) {
  return el(
    "details",
    { open: "" },
    el("summary", {}, source.source, " ", el("span", { class: "muted" }, source.type === "warp-remote" ? "remote" : "")),
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
      tr("Query this table"),
    ),
    columns,
  );
  return node;
}

async function loadColumns(name, list) {
  list.replaceChildren(el("li", { class: "muted" }, tr("Loading…")));
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
      ? exampleSql
      : tr("Ask in words, using the certified business terms");
  followable();
  questionHelp();
}

// Questions in words need certified terms: say so, and how to get there, before a run fails.
async function questionHelp(force = false) {
  const box = $("question-help");
  box.hidden = true;
  if (mode !== "question") return;
  try {
    const ready = await api("POST", "/olap/tools/check_readiness", {});
    const terms = (ready.checks || []).find((c) => c.check === "certified_terms");
    if (!force && terms && terms.tables > 0) return;
  } catch {
    if (!force) return; // the run itself will say what is wrong
  }
  if (mode !== "question") return;
  box.replaceChildren(
    el("p", {}, el("strong", {}, tr("Questions cannot be answered yet.")), " ", tr("They are read with the business terms of an approved catalog, and none is certified.")),
    el(
      "ol",
      { class: "plain" },
      el("li", {}, tr("In Catalog, analyze a database: a model drafts descriptions and terms.")),
      el("li", {}, tr("Review the tables and correct what is wrong.")),
      el("li", {}, tr("Approve the tables and certify their terms.")),
    ),
    el("div", { class: "bar" }, el("button", { type: "button", class: "secondary small", onclick: () => $("tab-catalog").click() }, tr("Open Catalog")), el("span", { class: "muted" }, tr("Until then, SQL works on every table."))),
  );
  box.hidden = false;
}

async function run() {
  const text = $("input").value.trim();
  if (!text) {
    $("status").textContent = mode === "sql" ? tr("Write a query first.") : tr("Write a question first.");
    return $("input").focus();
  }
  $("run").disabled = true;
  $("status").textContent = tr("Running…");
  $("notes").replaceChildren();
  $("result").replaceChildren();
  rememberQuery(mode, text);
  try {
    const data =
      mode === "sql"
        ? await api("POST", "/olap/query", { sql: text })
        : await api("POST", "/olap/tools/answer_question", {
            question: text,
            ...($("follow").checked && lastRequest ? { previous: lastRequest } : {}),
          });
    showResult(data);
  } catch (e) {
    $("status").textContent = "";
    if (mode === "question" && /needs a reviewed vocabulary/.test(e.message)) {
      // The steps above say what to do; the server's wording is for the command line.
      $("result").replaceChildren(el("p", { class: "error" }, tr("No term is certified yet, so this question cannot be read. The steps are above.")));
      await questionHelp(true);
    } else {
      $("result").replaceChildren(el("pre", { class: "error" }, e.message));
      if (mode === "question") await showUnread(text);
    }
  } finally {
    $("run").disabled = false;
  }
}

// The words a question was answered without, or refused for: for a key that
// may edit catalogs, each comes with a way to add it, and the question is
// asked again.
async function unreadNote(words, answered) {
  const catalogs = await glossaries(); // audit.js: empty unless the key may edit
  const what = answered
    ? tr`Answered without: ${words.join(", ")}. Check the reading still asks what you meant.`
    : tr`Not in the vocabulary: ${words.join(", ")}.`;
  const how = catalogs.length
    ? tr(" Add a word where it belongs and the question is asked again.")
    : tr(" A key that may edit catalogs can add them here, or from the Audit tab.");
  return [
    el("p", { class: answered ? "warn" : "muted" }, what + how),
    ...(catalogs.length
      ? words.map((word) => el("div", { class: "unread" }, el("code", {}, word), writeOut(word, catalogs, run, "status")))
      : []),
  ];
}

// After a question was not answered: which words were not read.
async function showUnread(text) {
  try {
    const check = await api("POST", "/olap/tools/check_question", { question: text });
    if (check.unknown?.length) $("result").append(...(await unreadNote(check.unknown, false)));
  } catch {
    // The check is a help, not the answer: its failure says nothing new.
  }
}

function note(title, text, open = false) {
  return el("details", { open: open || null }, el("summary", {}, title), el("pre", {}, text));
}

function cell(value) {
  if (value === null || value === undefined) return el("td", { class: "null" }, "NULL");
  if (typeof value === "number") return el("td", { class: "num" }, String(value));
  return el("td", {}, typeof value === "object" ? JSON.stringify(value) : String(value));
}

function followable() {
  $("follow-label").hidden = mode !== "question" || !lastRequest;
}

function showResult(data) {
  // Only a reading can be followed up: not SQL, nor SQL a model wrote.
  lastRequest = data.request || null;
  followable();
  const parts = [tr`${data.row_count} row${data.row_count === 1 ? "" : "s"}`];
  if (data.truncated) parts.push(tr`first ${data.rows.length} shown`);
  parts.push(tr`${data.execution_time_ms} ms`);
  if (data.from_cache) parts.push(tr("from cache"));
  $("status").textContent = parts.join(" · ");

  const notes = [];
  const byModel = data.read_by === "model";
  if (byModel) notes.push(el("p", { class: "warn" }, data.note || tr("Written by a model, not read from the vocabulary.")));
  if (data.request) notes.push(note(tr("Understood as"), JSON.stringify(data.request, null, 2)));
  if (data.sql) notes.push(note(byModel ? tr("SQL a model wrote") : tr("Compiled SQL"), data.sql, byModel));
  if (data.literal_repairs) notes.push(note(tr("Literal repairs"), JSON.stringify(data.literal_repairs, null, 2)));
  if (data.grounding) notes.push(note(tr("Join check"), JSON.stringify(data.grounding, null, 2)));
  $("notes").replaceChildren(...notes);
  const shown = ++results; // a slower note must not land on a newer answer
  if (data.unread?.length)
    unreadNote(data.unread, !byModel).then((parts) => shown === results && $("notes").prepend(...parts));

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

const VIEWS = ["query", "catalog", "audit", "keys", "databases", "settings", "docs"];
let currentView = "query";

function showView(name) {
  const moved = name !== currentView;
  currentView = name;
  for (const view of VIEWS) {
    $(`view-${view}`).hidden = view !== name;
    // is-active alone: Carbon drops the "current" style when aria-current="page" is set.
    $(`tab-${view}`).toggleAttribute("is-active", view === name);
    $(`side-${view}`)?.toggleAttribute("active", view === name);
  }
  const menu = document.querySelector("cds-side-nav");
  if (menu && window.matchMedia("(max-width: 1055px)").matches) menu.expanded = false;
  document.title = tr`${$(`tab-${name}`).textContent.trim()} · Truejoin console`;
  // The address says where you are: a reload comes back here, and Back goes back.
  if (location.hash.slice(1) !== name) history.pushState(null, "", `#${name}`);
  if (moved) $(`view-${name}`).focus({ preventScroll: true });
}

// Each side-navigation link opens the view its header item opens.
for (const view of VIEWS) {
  $(`side-${view}`)?.addEventListener("click", (e) => {
    e.preventDefault();
    $(`tab-${view}`).click();
  });
}

// The view the address names (a reload, a shared link, Back and Forward): opened as a
// click on its header item would, so each view's own loading runs.
function route() {
  const name = location.hash.slice(1);
  if (name === "main") return $("main").querySelector(".view:not([hidden])")?.focus();
  if (VIEWS.includes(name) && name !== currentView) $(`tab-${name}`).click();
  else if (!name && currentView !== "query") $("tab-query").click();
  document.title = tr`${$(`tab-${currentView}`).textContent.trim()} · Truejoin console`;
}
window.addEventListener("hashchange", route);
window.addEventListener("popstate", route);
window.addEventListener("DOMContentLoaded", route);

$("tab-query").addEventListener("click", () => showView("query"));
$("tab-catalog").addEventListener("click", () => showView("catalog"));
$("tab-audit").addEventListener("click", () => showView("audit"));
$("tab-keys").addEventListener("click", () => showView("keys"));
$("tab-docs").addEventListener("click", () => showView("docs"));
// Who the console is acting as: nobody is asked for a key when authentication is off,
// and a connected key shows its name and a way to disconnect.
async function whoAmI(asked = false) {
  const who = $("who");
  const key = $("key").value.trim();
  let access = null;
  let refused = false;
  try {
    await freshToken();
    const response = await fetch("/ui/access", { headers: withAuth() });
    if (response.ok) access = await response.json();
    else refused = response.status === 401;
  } catch {}
  const open = access && access.auth_enabled === false;
  const known = Boolean(access && access.actor);
  $("auth").hidden = open || known;
  $("disconnect").hidden = !known;
  who.className = "who";
  if (open) who.textContent = tr("Authentication is off: full access");
  else if (known) who.textContent = tr`Connected as ${access.actor}`;
  else if (key && refused) {
    who.className = "who error";
    who.textContent = tr("This key is not known");
  } else if (asked && !key) {
    who.className = "who error";
    who.textContent = tr("Enter a key first");
    $("key").focus();
  } else who.textContent = "";
  return access;
}

$("auth").addEventListener("submit", async (e) => {
  e.preventDefault();
  await whoAmI(true);
  loadSources();
});
$("connect").addEventListener("click", () => $("auth").requestSubmit());
$("key").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("auth").requestSubmit();
});
$("disconnect").addEventListener("click", async () => {
  $("key").value = "";
  remember("");
  keepSession(null);
  await whoAmI();
  // Every view asks again as nobody: what was on screen belonged to the key.
  $("auth").dispatchEvent(new Event("submit", { cancelable: true }));
});
$("shortcut").textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘ Enter" : "Ctrl+Enter";
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
// -- signing in with the identity provider (Authorization Code + PKCE) ------
// The page sends its user to the provider and back; this server exchanges the
// code for a token (so the page keeps talking to this server only), and the
// token then goes out as a bearer token like a pasted one.
const SIGN_IN = "truejoin-console-sign-in";

function base64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

async function startSignIn(config) {
  const verifier = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const state = base64url(crypto.getRandomValues(new Uint8Array(16)));
  const challenge = base64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  try {
    sessionStorage.setItem(SIGN_IN, JSON.stringify({ verifier, state }));
  } catch {
    return alert(tr("Signing in needs this tab's storage, which the browser refuses."));
  }
  const params = new URLSearchParams({
    response_type: "code",
    client_id: config.client_id,
    redirect_uri: config.redirect_uri,
    scope: config.scope,
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  location.assign(`${config.authorization_endpoint}?${params}`);
}

// Back from the provider with ?code&state (or ?error): finish, then drop them from the address bar.
async function finishSignIn() {
  const back = new URLSearchParams(location.search);
  if (!back.has("code") && !back.has("error")) return;
  history.replaceState(null, "", location.pathname);
  let started = {};
  try {
    started = JSON.parse(sessionStorage.getItem(SIGN_IN) || "{}");
    sessionStorage.removeItem(SIGN_IN);
  } catch {}
  const say = (text) => $("tables").replaceChildren(el("p", { class: "error" }, text));
  if (back.has("error")) return say(tr`Sign-in refused: ${back.get("error_description") || back.get("error")}`);
  if (!started.state || started.state !== back.get("state")) return say(tr("Sign-in did not start in this tab; try again."));
  try {
    const response = await fetch("/ui/sign-in/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: back.get("code"), code_verifier: started.verifier }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) return say(tr`Sign-in failed: ${data.detail || response.statusText}`);
    $("key").value = data.access_token;
    remember(data.access_token);
    keepSession(data);
  } catch (e) {
    say(tr`Sign-in failed: ${e.message}`);
  }
}

async function offerSignIn() {
  try {
    const config = await (await fetch("/ui/sign-in")).json();
    if (!config.enabled) return;
    $("sign-in").hidden = false;
    $("sign-in").addEventListener("click", () => startSignIn(config));
  } catch {}
}

$("key").value = recalled();
scheduleRefresh();
setMode("sql");
offerSignIn();
finishSignIn().then(() => whoAmI()).then(loadSources);

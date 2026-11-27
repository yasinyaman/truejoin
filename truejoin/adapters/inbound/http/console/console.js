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
    // A connection that failed says why as a reason, with the driver's own words.
    error.reason = data.reason || "";
    error.raw = data.raw || "";
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
    if (e.status === 401) return welcome(nav);
    if (e.status === 403) {
      return nav.replaceChildren(
        el("div", { class: "welcome" }, el("p", {}, el("strong", {}, tr("This key cannot query."))), el("p", {}, tr("It lacks the query permission: ask an administrator for a key that has it."))),
      );
    }
    nav.replaceChildren(el("p", { class: "error" }, e.message));
  }
}

// Nobody is connected: say how to come in, with the ways this server offers, and
// which databases it serves (the public /health already says so; it may not).
async function welcome(nav) {
  const ways = [];
  const sign = await signInOffered;
  let databases = [];
  try {
    const health = await (await fetch("/health")).json();
    databases = Object.keys(health.databases || {});
  } catch {}
  if (sign.password) ways.push(el("button", { type: "button", class: "secondary small", onclick: () => $("sign-in-password").click() }, tr("Sign in with a password")));
  if (sign.enabled) ways.push(el("button", { type: "button", class: "secondary small", onclick: () => $("sign-in").click() }, tr("Sign in")));
  nav.replaceChildren(
    el(
      "div",
      { class: "welcome" },
      el("p", {}, el("strong", {}, tr("Sign in to see the databases."))),
      el("p", {}, ways.length ? tr("Sign in, or paste an API key in the field at the top and press Connect.") : tr("Paste an API key in the field at the top and press Connect.")),
      ways.length ? el("div", { class: "bar" }, ...ways) : null,
      databases.length ? el("p", { class: "muted" }, tr`This server serves: ${databases.join(", ")}.`) : null,
    ),
  );
}

function sourceNode(source) {
  return el(
    "details",
    { open: "" },
    el("summary", {}, source.source, " ", el("span", { class: "muted" }, source.type === "warp-remote" ? tr("Remote") : "")),
    ...source.tables.map(tableNode),
  );
}

function tableNode(table) {
  const columns = el("ul", { class: "cols" });
  const rows = el("span", { class: "muted" }, rowsLabel(table));
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
      rows,
      table.loaded ? el("span", { class: "badge" }, tr("Loaded")) : null,
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
    // Held in memory: an administrator may read it again now, not at the next timed refresh.
    table.loaded && ACCESS.admin ? reloadButton(table, rows) : null,
    columns,
  );
  return node;
}

function reloadButton(table, rows) {
  const said = el("p", { class: "muted reload-said", role: "status" });
  const button = el(
    "button",
    {
      type: "button",
      class: "link",
      onclick: async () => {
        button.disabled = true;
        said.className = "muted reload-said";
        said.textContent = tr("Loading…");
        try {
          const done = await api("POST", `/olap/tables/${encodeURIComponent(table.name)}/refresh`);
          if (done.row_count >= 0) table.row_count = done.row_count;
          rows.textContent = rowsLabel(table);
          said.textContent = tr("Read again from the database.");
        } catch (e) {
          said.className = "error reload-said";
          said.textContent = e.message;
        } finally {
          button.disabled = false;
        }
      },
    },
    tr("Reload"),
  );
  return el("span", {}, button, said);
}

async function loadColumns(name, list) {
  list.replaceChildren(el("li", { class: "muted" }, tr("Loading…")));
  try {
    const schema = await api("GET", `/olap/tables/${encodeURIComponent(name)}/schema`);
    const keys = new Set(schema.primary_key || []);
    list.replaceChildren(
      ...(schema.columns || []).map((c) =>
        el("li", {}, c.name, keys.has(c.name) ? el("span", { class: "badge" }, tr("Key")) : null, " ",
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
    if (!force && terms && terms.tables > 0) return askable(box);
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
    el("div", { class: "bar" }, el("button", { type: "button", class: "secondary small", onclick: () => go("catalog") }, tr("Open Catalog")), el("span", { class: "muted" }, tr("Until then, SQL works on every table."))),
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
  $("question-help").hidden = mode !== "question" || !$("question-help").childElementCount;
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
    } else if (mode === "question" && e.message.startsWith(UNREAD)) {
      await showRefusal(text);
    } else if (mode === "question" && e.message.startsWith(APART)) {
      await showApart(text);
    } else {
      $("result").replaceChildren(el("pre", { class: "error" }, e.message));
      if (mode === "question") await showUnread(text);
    }
  } finally {
    $("run").disabled = false;
  }
}

// How the server starts refusing a question none of whose words it read
// (application/olap/tools.py _UNREAD): the rest of that message is for models.
const UNREAD = "Nothing in this question";
// And refusing one whose words it knows, but which name tables no one reading joins (_APART).
const APART = "This question names more than one reading can hold";

// A word with the place it was recognised as, when it names one: "sipariş (primary_db.orders)".
function recognised(w) {
  const places = w.read_as.filter((r) => /^(?:table|column|value) /.test(r)).map((r) => r.replace(/^\w+ /, ""));
  return places.length ? `${w.word} (${places.join(", ")})` : w.word;
}

// What a model proposed for a word, and where a word was added, kept while
// the page is: the question is asked again after every word, and the rows
// are drawn again with it.
const proposed = new Map();
const placed = new Map();

// A list of names short enough to read in a line: the first few, and how many more.
function someOf(names, most = 10) {
  return names.length > most ? tr`${names.slice(0, most).join(", ")} and ${names.length - most} more` : names.join(", ");
}

function toCatalog(db) {
  go("catalog", db);
}

// What the vocabulary covers, as far as this key may read the catalogs:
// the terms questions are read with, and the drafts nobody certified yet.
function vocabularyFacts(catalogs, brief = false) {
  const lines = [];
  const approved = catalogs.filter((c) => c.status === "approved");
  const certified = approved.flatMap((c) => c.terms.filter((t) => t.certified).map((t) => `${t.name} (${c.db})`));
  if (certified.length) lines.push(el("p", {}, el("strong", {}, tr("Questions are read with these terms:")), " ", someOf(certified), "."));
  for (const c of catalogs) {
    const drafts = c.terms.filter((t) => !t.certified).map((t) => t.name);
    const waits =
      c.status !== "approved"
        ? tr`${c.db}: the catalog waits for approval, so nothing in it is read yet.`
        : drafts.length
          ? tr`${c.db}: ${drafts.length} draft term(s) wait to be certified (${someOf(drafts)}). Nothing is read with a draft.`
          : "";
    if (waits) lines.push(el("p", {}, waits, " ", el("button", { type: "button", class: "secondary small", onclick: () => toCatalog(c.db) }, tr("Review in Catalog"))));
  }
  if (brief) return lines;
  const languages = [...new Set(approved.flatMap((c) => c.languages))];
  if (languages.length) lines.push(el("p", { id: "word-languages" }, tr`The words of the approved catalogs are in: ${languages.join(", ")}. A question in another language finds none of them.`));
  return lines;
}

// In question mode, before anything is asked: what there is to ask about.
async function askable(box) {
  const lines = vocabularyFacts(await vocabulary(), true); // audit.js
  if (mode !== "question" || !lines.length) return;
  box.replaceChildren(...lines);
  box.hidden = false;
}

// For an administrator: the settings that would have let this question be read.
async function settingHints() {
  const hints = { language: null, model: null };
  try {
    if (!ACCESS.admin) return hints;
    const rows = (await api("GET", `${API_PREFIX}/settings`)).settings || [];
    const row = (path) => rows.find((r) => r.path === path);
    const open = (r, label) =>
      r.locked
        ? el("span", { class: "muted" }, tr("Locked in the configuration file."))
        : el("button", { type: "button", class: "secondary small", onclick: () => showSetting(r.path) }, label); // settings.js
    const languages = row("settings.i18n.languages");
    if (languages) hints.language = open(languages, tr("Add a language"));
    const unread = row("olap.unread_questions");
    if (unread && unread.value === "refuse")
      hints.model = el("p", {}, tr("A model can answer such questions by writing SQL from the schema. That is off."), " ", open(unread, tr("Open the setting")));
  } catch {
    // No settings to point at: the facts above stand without them.
  }
  return hints;
}

// A question none of whose words were read: why, in this console's words,
// what the vocabulary does cover, and the ways forward.
async function showRefusal(text) {
  const mine = ++results; // a slower answer must not land on a newer run
  const box = $("notes"); // not the result's scroll box: this is read down the page
  box.replaceChildren(el("p", { class: "error" }, tr("None of this question's words are in the certified vocabulary, so it was not answered.")));
  const [check, catalogs, hints] = await Promise.all([
    api("POST", "/olap/tools/check_question", { question: text }).catch(() => null),
    vocabulary(), // audit.js
    settingHints(),
  ]);
  if (mine !== results) return;
  // The same facts stand above the question: here they are fresh, and next to the refusal.
  const above = $("question-help");
  above.replaceChildren(...vocabularyFacts(catalogs, true));
  above.hidden = true;
  const words = (check?.words || []).filter((w) => !w.read_as.length).map((w) => w.written || w.word);
  const facts = el("div", { class: "facts" }, ...vocabularyFacts(catalogs));
  if (hints.language) facts.querySelector("#word-languages")?.append(" ", hints.language);
  if (hints.model) facts.append(hints.model);
  box.append(facts);
  if (words.length) box.append(...(await unreadNote(words, false, catalogs)));
}

// A question whose words are known, but name tables no single reading joins:
// what it would have been read from, and what that would have left out. No
// word is offered to add: the vocabulary is not what is missing.
async function showApart(text) {
  const mine = ++results; // a slower answer must not land on a newer run
  const box = $("notes");
  box.replaceChildren(el("p", { class: "error" }, tr("This question names things that no single reading connects, so it was not answered.")));
  const [check, hints] = await Promise.all([api("POST", "/olap/tools/check_question", { question: text }).catch(() => null), settingHints()]);
  if (mine !== results) return;
  const facts = el("div", { class: "facts" });
  const apart = check?.apart || [];
  if (apart.length && check.reading)
    facts.append(el("p", {}, tr`Read from ${check.reading.table}, it would have left out: ${apart.map(recognised).join(", ")}. That table does not reference them, so the answer would have been to another question.`));
  facts.append(el("p", {}, tr("Adding words does not change this. Ask about one thing at a time, or write it in SQL.")));
  if (hints.model) facts.append(hints.model);
  box.append(facts);
}

// Asks a model where the words belong and points each row there; nothing is added.
async function proposePlaces(words, redraw, say) {
  say(tr("Asking a model where these belong…"));
  try {
    const found = await api("POST", `${API_PREFIX}/catalog/suggest-words`, { words: words.slice(0, 20), lang: LANG });
    for (const s of found.suggestions) proposed.set(s.word, s);
    redraw();
    say(found.suggestions.length ? tr`A place proposed for ${found.suggestions.length} of ${words.length} word(s). Check each, then add.` : tr("The model found no place for these words."));
  } catch (e) {
    say(e.message, true);
  }
}

// The words a question was answered without, or refused for: for a key that
// may edit catalogs, each comes with a way to add it, and the question is
// asked again. A model can propose the places; a person adds them.
async function unreadNote(words, answered, known) {
  const catalogs = await glossaries(known); // audit.js: empty unless the key may edit
  const what = answered
    ? tr`Answered without: ${words.join(", ")}. Check the reading still asks what you meant.`
    : catalogs.length
      ? ""
      : tr`Not in the vocabulary: ${words.join(", ")}.`;
  const how = catalogs.length
    ? tr(" Add a word where it belongs and the question is asked again.")
    : tr(" A key that may edit catalogs can add them here, or from Audit in Administration.");
  const head = el("p", { class: answered ? "warn" : "muted" }, (what + how).trim());
  if (!catalogs.length) return [head];

  const list = el("div", { class: "unread-list" });
  const status = el("span", { class: "muted", role: "status" });
  const say = (text, error = false) => {
    status.className = error ? "error" : "muted";
    status.textContent = text;
  };
  // Filled only while a proposal waits: a Carbon button made hidden comes back without its label.
  const more = el("span");
  let forms = [];
  const addProposed = async () => {
    for (const [word, form] of forms) {
      const added = await form.add();
      if (added) placed.set(word, added);
    }
    await run();
  };
  const draw = () => {
    forms = [];
    list.replaceChildren(
      ...words.map((word) => {
        const form = writeOut(word, catalogs, run, "status");
        form.addEventListener("word-added", (e) => e.detail && placed.set(word, e.detail));
        const idea = proposed.get(word);
        const fits = idea ? form.propose(idea) : false;
        if (fits && !placed.has(word)) forms.push([word, form]);
        const about = placed.has(word)
          ? el("span", { class: "muted" }, el("span", { class: "badge tag-green" }, tr("Added")), " ", placed.get(word))
          : fits
            ? el("span", { class: "muted" }, el("span", { class: "badge tag-blue" }, tr("Proposed")), " ", idea.why)
            : null;
        return el("div", { class: "unread" }, el("code", {}, word), form, el("span", { class: "about" }, about));
      }),
    );
    more.replaceChildren(...(forms.length ? [el("button", { type: "button", class: "secondary small", onclick: addProposed }, tr("Add the proposed"))] : []));
  };
  draw();
  return [
    head,
    el("div", { class: "bar" }, el("button", { type: "button", class: "secondary small", onclick: () => proposePlaces(words, draw, say) }, tr("Propose places")), more, status),
    list,
  ];
}

// After a question was not answered: which words were not read.
async function showUnread(text) {
  try {
    const check = await api("POST", "/olap/tools/check_question", { question: text });
    const words = (check.words || []).filter((w) => !w.read_as.length).map((w) => w.written || w.word);
    if (words.length) $("result").append(...(await unreadNote(words, false)));
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
  // Known words the reading made no use of: said first, as the answer may be to less than was asked.
  if (data.unused?.length)
    notes.push(el("p", { class: "warn" }, tr`Recognised, but not used in this reading: ${data.unused.map(recognised).join(", ")}. Adding words does not change that: check the reading, ask more simply, or write it in SQL.`));
  // A join that matched no key: corrected before it ran, or run as written and said so.
  const joins = data.grounding?.joins || [];
  const fixed = joins.filter((j) => j.repaired);
  const doubted = joins.filter((j) => !j.repaired);
  if (fixed.length) notes.push(el("p", { class: "warn" }, tr`The join was corrected before it ran: ${fixed.map((j) => j.message).join("; ")}. The rows are those of the SQL that ran, below.`));
  if (doubted.length) notes.push(el("p", { class: "warn" }, tr`A join could not be vouched for, and ran as written: ${doubted.map((j) => j.message).join("; ")}.`));
  if (data.masked?.length) notes.push(el("p", { class: "muted" }, tr`Masked for you: ${data.masked.join(", ")}. Your key sees these columns as the masking rules show them.`));
  if (data.request && data.labels) notes.push(el("p", { class: "reading" }, reading(data.request, data.labels)));
  if (data.grounding?.sql) notes.push(note(tr("SQL that ran"), data.grounding.sql, true));
  if (data.request) notes.push(note(data.labels ? tr("Understood as (the request)") : tr("Understood as"), JSON.stringify(data.request, null, 2)));
  if (data.sql) notes.push(note(byModel ? tr("SQL a model wrote") : data.grounding?.sql ? tr("Compiled SQL (before the join check)") : tr("Compiled SQL"), data.sql, byModel));
  if (data.literal_repairs) notes.push(note(tr("Literal repairs"), JSON.stringify(data.literal_repairs, null, 2)));
  if (data.grounding) notes.push(note(tr("Join check"), JSON.stringify(data.grounding, null, 2)));
  $("notes").replaceChildren(...notes);
  const shown = ++results; // a slower note must not land on a newer answer
  if (data.unread?.length)
    unreadNote(data.unread_written || data.unread, !byModel).then((parts) => shown === results && $("notes").prepend(...parts));

  const columns = data.columns || [];
  const rows = data.rows || [];
  const table = el(
    "table",
    {},
    el("thead", {}, el("tr", {}, ...columns.map((c) => heading(c, data.labels, data.masked)))),
    el("tbody", {}, ...rows.map((row) => el("tr", {}, ...columns.map((c) => cell(row[c]))))),
  );
  $("result").replaceChildren(...(columns.length ? resultViews(columns, rows, table) : [table]));
}

// -- pages and their addresses ------------------------------------------------
// Every screen is a page: it registers what to do when its address is opened, and
// the address (the hash) says what is on screen, so a reload, a shared link, Back
// and Forward all come back to it: #catalog/smoke/musteriler, #admin/settings/masking.
//
//   page("keys", { needs: "admin", area: "admin", open(args, moved) {…}, stale() {…} })
//
// `needs` is the permission without which the page is not offered; `area` puts it
// in Administration, one screen whose side panel lists its pages; `open` runs
// whenever the address names the page (again, too: opening where you already are
// refreshes it); `stale` when the caller changes, to forget what the last one saw.
const PAGES = {};
let currentView = "query";

function page(name, spec) {
  PAGES[name] = spec;
}

// Administration's pages, in the order its side panel lists them.
const ADMIN = ["databases", "keys", "users", "audit", "settings"];
const LAST_ADMIN = "truejoin-console-admin";

// The header item, the view and the menu links a page is reached and shown by.
function tabOf(name) {
  return $(`tab-${PAGES[name].area || name}`);
}

function viewOf(name) {
  return $(PAGES[name].area ? `view-${PAGES[name].area}` : `view-${name}`);
}

// What a page is called: its link's own text, as the page shows it.
function nameOf(name) {
  const link = PAGES[name].area ? $(`nav-${name}`) : tabOf(name);
  return (link.querySelector("[data-t]") || link).textContent.trim(); // not a count beside it
}

function titleOf(name) {
  if (!PAGES[name].area) return tr`${nameOf(name)} · Truejoin console`;
  return tr`${nameOf(name)} · ${tabOf(name).textContent.trim()} · Truejoin console`;
}

// What /ui/access says the caller may do: read, query, update, create, audit, admin,
// with who they are. A hint for what to offer; every route still checks for itself.
let ACCESS = {};

function signedOut() {
  return ACCESS.auth_enabled !== false && !ACCESS.actor;
}

// Whether the caller may open a page: it names the permission it `needs`, if any.
function may(name) {
  if (signedOut()) return false;
  const needs = PAGES[name].needs;
  return !needs || ACCESS[needs] === true;
}

// Only what the caller may open is offered: a screen that would answer
// "you may not" is not in the menu at all. Administration is offered when any
// of its pages is, and lists those alone.
function applyAccess() {
  for (const name of Object.keys(PAGES)) {
    const hidden = !may(name);
    if (!PAGES[name].area) tabOf(name).hidden = hidden;
    for (const link of [$(`side-${name}`), $(`nav-${name}`)]) if (link) link.hidden = hidden;
  }
  const none = !ADMIN.some((name) => PAGES[name] && may(name));
  $("tab-admin").hidden = none;
  $("side-admin").hidden = none;
}

// Where a visit with no address starts: the first screen the caller may open.
const LANDING = ["query", "catalog", ...ADMIN, "docs"];

function landing() {
  return LANDING.find((name) => PAGES[name] && may(name)) || "query";
}

// Where bare #admin goes: the page it was left on, if it may still be opened, else its first.
function adminHome() {
  let last = null;
  try {
    last = sessionStorage.getItem(LAST_ADMIN);
  } catch {}
  if (ADMIN.includes(last) && PAGES[last] && may(last)) return last;
  return ADMIN.find((name) => PAGES[name] && may(name)) || ADMIN[0];
}

function clearCurrent() {
  for (const name of Object.keys(PAGES)) {
    viewOf(name).hidden = true;
    tabOf(name).removeAttribute("is-active");
    $(`side-${name}`)?.removeAttribute("active");
  }
}

// In place of a page the caller may not open: how to sign in when nobody is, or
// which permission it needs. The address stays, so signing in opens what was asked for.
function showGate(name) {
  clearCurrent();
  document.title = titleOf(name);
  $("view-gate").hidden = false;
  if (signedOut()) return welcome($("gate"));
  return $("gate").replaceChildren(
    el(
      "div",
      { class: "welcome" },
      el("p", {}, el("strong", {}, tr`This key cannot open ${nameOf(name)}.`)),
      el("p", {}, tr`It needs the ${PAGES[name].needs} permission: ask an administrator for a key that has it.`),
    ),
  );
}

// A page's address: its name and what it shows; Administration's pages under #admin.
function hashOf(name, ...args) {
  const parts = [PAGES[name]?.area, name, ...args].filter((part) => part !== null && part !== undefined && part !== "");
  return `#${parts.map(encodeURIComponent).join("/")}`;
}

// Go to a page as a click on a link to it would: one more step in the history.
function go(name, ...args) {
  const hash = hashOf(name, ...args);
  if (location.hash === hash) navigate();
  else location.hash = hash; // navigate() follows, on hashchange
}

// Say where the page is now, without opening anything: for what a screen shows on
// its own (a table picked in a diagram). `replace` when it is the same place, named fully.
function address(name, args, replace = false) {
  const hash = hashOf(name, ...args);
  if (location.hash === hash) return;
  if (replace) history.replaceState(null, "", hash);
  else history.pushState(null, "", hash);
}

function showView(name) {
  const moved = name !== currentView || !$("view-gate").hidden;
  currentView = name;
  $("view-gate").hidden = true;
  $("gate").replaceChildren();
  clearCurrent();
  viewOf(name).hidden = false;
  // is-active alone: Carbon drops the "current" style when aria-current="page" is set.
  tabOf(name).setAttribute("is-active", "");
  $(`side-${name}`)?.setAttribute("active", "");
  for (const section of ADMIN) {
    $(`pane-${section}`).hidden = section !== name;
    $(`nav-${section}`).classList.toggle("current", section === name);
    if (section === name) $(`nav-${section}`).setAttribute("aria-current", "page");
    else $(`nav-${section}`).removeAttribute("aria-current");
  }
  $("settings-nav").hidden = name !== "settings";
  if (PAGES[name].area === "admin") {
    // Where the pages run across a narrow window, the current one is brought into sight.
    $(`nav-${name}`).scrollIntoView({ block: "nearest", inline: "nearest" });
    try {
      sessionStorage.setItem(LAST_ADMIN, name);
    } catch {}
  }
  const menu = document.querySelector("cds-side-nav");
  if (menu && window.matchMedia("(max-width: 1055px)").matches) menu.expanded = false;
  document.title = titleOf(name);
  if (moved) {
    window.scrollTo(0, 0); // each screen starts at its top
    (PAGES[name].area ? $(`pane-${name}`) : viewOf(name)).focus({ preventScroll: true });
  }
  return moved;
}

// Open what the address names: on load, on a link, on Back and Forward.
function navigate() {
  let [name = "", ...args] = location.hash.slice(1).split("/").map((part) => {
    try {
      return decodeURIComponent(part);
    } catch {
      return part;
    }
  });
  if (name === "admin") {
    // Bare #admin is wherever Administration was left; named in full, in place.
    if (!args.length) history.replaceState(null, "", hashOf(adminHome()));
    name = args.length ? args.shift() : adminHome();
    if (!ADMIN.includes(name)) return undefined;
  } else if (ADMIN.includes(name)) {
    // An address from before Administration (#keys, #settings/masking) still opens its page.
    history.replaceState(null, "", hashOf(name, ...args));
  }
  const target = PAGES[name] ? name : name ? null : landing();
  if (!target) return undefined; // an address this console does not have: stay where we are
  if (!may(target)) return showGate(target);
  const moved = showView(target);
  return PAGES[target].open?.(args, moved);
}
window.addEventListener("hashchange", navigate);

// A header item is a link to its page; pressed where you already are, it opens the page again.
document.querySelector("cds-header-nav").addEventListener("click", (e) => {
  const item = e.target.closest("cds-header-nav-item");
  if (item && location.hash === item.getAttribute("href")) navigate();
});
// The skip link moves the focus, not the address.
document.querySelector("a.skip").addEventListener("click", (e) => {
  e.preventDefault();
  (PAGES[currentView].area ? $(`pane-${currentView}`) : viewOf(currentView)).focus();
});

page("query", { needs: "query" });

// Who the console is acting as: nobody is asked for a key when authentication is off,
// and a connected key shows its name and a way to disconnect.
// Whether the sign-in in hand is a local user's: its token says so (http/auth.py).
// A hint for offering "Change password"; the route checks for itself.
function localAccount() {
  const parts = $("key").value.trim().split(".");
  if (parts.length !== 3) return false;
  try {
    return JSON.parse(atob(parts[1].replaceAll("-", "+").replaceAll("_", "/"))).src === "local";
  } catch {
    return false;
  }
}

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
  ACCESS = access || {};
  applyAccess();
  $("auth").hidden = open || known;
  // Who is connected is the account menu's name; a refusal goes in the bar under
  // the header, where there is room for it at any width.
  $("account").hidden = !known;
  $("account").open = false;
  $("account-name").textContent = known ? access.actor : "";
  $("account-name").title = known ? tr`Connected as ${access.actor}` : "";
  $("change-password").hidden = !(known && localAccount());
  who.textContent = open ? tr("Authentication is off: full access") : "";
  $("refused").textContent = "";
  if (open || known) return access;
  if (key && refused) $("refused").textContent = tr("This key is not known");
  else if (asked && !key) {
    $("refused").textContent = tr("Enter a key first");
    $("key").focus();
  }
  return access;
}

// Somebody else is calling now (or nobody): what the last caller saw is forgotten,
// and the page the address names is opened as this one may see it.
function callerChanged() {
  for (const spec of Object.values(PAGES)) spec.stale?.();
  if (may("query")) loadSources();
  navigate();
}

// Let go of the sign-in in hand, saying why in the bar under the header if there is a reason.
async function signOut(why = "") {
  $("key").value = "";
  remember("");
  keepSession(null);
  await whoAmI();
  callerChanged();
  $("refused").textContent = why;
}

$("auth").addEventListener("submit", async (e) => {
  e.preventDefault();
  await whoAmI(true);
  // What was on screen belonged to the caller before: the sources, and the page shown.
  callerChanged();
});
$("connect").addEventListener("click", () => $("auth").requestSubmit());
$("key").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("auth").requestSubmit();
});
$("disconnect").addEventListener("click", () => signOut());
$("account-menu").addEventListener("click", () => ($("account").open = false));
document.addEventListener("click", (e) => {
  if (!$("account").contains(e.target)) $("account").open = false;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("account").open) {
    $("account").open = false;
    $("account-name").focus();
  }
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
    sessionStorage.setItem(SIGN_IN, JSON.stringify({ verifier, state, hash: location.hash }));
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
  let started = {};
  try {
    started = JSON.parse(sessionStorage.getItem(SIGN_IN) || "{}");
    sessionStorage.removeItem(SIGN_IN);
  } catch {}
  // Back where the visitor was when they left for the provider.
  history.replaceState(null, "", location.pathname + (typeof started.hash === "string" ? started.hash : ""));
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

// -- signing in with a directory password (lldap, OpenLDAP, Active Directory) ---
// The directory checks the password on the server; what comes back is a token
// this server signed, used as a bearer token like the provider's.
async function passwordSignIn() {
  let note = "";
  for (;;) {
    const entered = await askLogin(note); // carbon.js: null when cancelled
    if (!entered) return;
    try {
      const response = await fetch("/ui/sign-in/password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(entered),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        note = data.detail || response.statusText;
        continue; // ask again, saying why
      }
      $("key").value = data.access_token;
      remember(data.access_token);
      keepSession(null); // no refresh token: when this one ends, the next call says to sign in
      $("auth").dispatchEvent(new Event("submit", { cancelable: true }));
      return;
    } catch (e) {
      note = e.message;
    }
  }
}

// What /ui/sign-in offered: {enabled, password}; empty until it answers or when it fails.
let offered = {};
let signInOffered = Promise.resolve(offered);

async function offerSignIn() {
  try {
    const config = await (await fetch("/ui/sign-in")).json();
    offered = config;
    // Each way in has two buttons, in the header and in the bar under it; the
    // stylesheet shows the one there is room for.
    const offer = (how, start) => {
      for (const button of document.querySelectorAll(`[data-sign-in="${how}"]`)) {
        button.hidden = false;
        button.addEventListener("click", start);
      }
    };
    if (config.enabled) offer("provider", () => startSignIn(config));
    if (config.password) offer("password", passwordSignIn);
  } catch {}
  return offered;
}

$("key").value = recalled();
scheduleRefresh();
setMode("sql");
signInOffered = offerSignIn();
// Who is calling is known first, then every script's pages: the address is opened
// once, as this caller may see it.
const pagesReady = new Promise((resolve) => window.addEventListener("DOMContentLoaded", resolve, { once: true }));
finishSignIn()
  .then(() => whoAmI())
  .then(() => pagesReady)
  .then(() => {
    applyAccess();
    if (may("query")) loadSources();
    navigate();
  });

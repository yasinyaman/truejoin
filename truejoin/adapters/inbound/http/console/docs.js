"use strict";
// API and MCP reference, read live from this server: /openapi.json and the MCP
// endpoint's own tools/list. Uses el, api and $ from console.js.

const MCP_PATH = document.querySelector('meta[name="truejoin-mcp-path"]').content;
// `page` is what the body shows: a chapter of the guide (guide.js), an MCP page or api:<tag>.
// `part` is the section of a chapter the address names, if it names one.
const docs = { spec: null, tools: null, toolsError: null, page: "guide-start", part: null };
const METHODS = ["get", "post", "put", "patch", "delete"];

function typeOf(schema = {}) {
  if (schema.$ref) return schema.$ref.split("/").pop();
  if (schema.anyOf) return schema.anyOf.map(typeOf).join(" | ");
  if (schema.type === "array") return `${typeOf(schema.items)}[]`;
  return schema.type || "any";
}

function resolve(schema) {
  const name = schema?.$ref?.split("/").pop();
  return name ? docs.spec.components?.schemas?.[name] || schema : schema || {};
}

// One row per property: name, type, required, description.
function fieldsTable(schema, nameHead = tr("Field")) {
  const props = Object.entries(schema.properties || {});
  if (!props.length) return el("p", { class: "muted" }, tr("No fields."));
  const required = new Set(schema.required || []);
  return el(
    "div",
    { class: "scroll" },
    el(
      "table",
      { class: "review" },
      el("thead", {}, el("tr", {}, ...[nameHead, tr("Type"), tr("Required"), tr("Description")].map((h) => el("th", { scope: "col" }, h)))),
      el(
        "tbody",
        {},
        ...props.map(([name, p]) =>
          el(
            "tr",
            {},
            el("td", {}, el("code", {}, name)),
            el("td", {}, typeOf(p)),
            el("td", {}, required.has(name) ? "yes" : ""),
            el("td", {}, p.description || ""),
          ),
        ),
      ),
    ),
  );
}

// A starting body: the required fields, each with an empty value of its type.
function sample(schema, depth = 0) {
  schema = resolve(schema);
  if (schema.example !== undefined) return schema.example;
  if (schema.default !== undefined) return schema.default;
  if (schema.anyOf) return sample(schema.anyOf.find((s) => s.type !== "null") || {}, depth);
  if (schema.type === "object" || schema.properties) {
    if (depth > 3) return {};
    return Object.fromEntries((schema.required || []).map((k) => [k, sample(schema.properties?.[k] || {}, depth + 1)]));
  }
  return { array: [], integer: 0, number: 0, boolean: false, string: "" }[schema.type] ?? null;
}

// Sends the request with the key above, so it does what that key may do and nothing more.
function tryIt(path, method, op) {
  const inputs = (op.parameters || [])
    .filter((p) => p.in === "path" || p.in === "query")
    .map((p) => [p, el("input", { class: "field", "aria-label": p.name, placeholder: p.required ? "required" : "optional" })]);
  const schema = op.requestBody?.content?.["application/json"]?.schema;
  const body = schema ? el("textarea", { class: "field", rows: "6", "aria-label": tr("Body (JSON)"), spellcheck: "false" }) : null;
  if (body) body.value = JSON.stringify(sample(schema), null, 2);
  const out = el("div", { "aria-live": "polite" });
  const send = el("button", { type: "submit" }, tr("Send"));
  const form = el(
    "form",
    { class: "analyze" },
    ...inputs.map(([p, input]) => el("label", {}, `${p.name} (${p.in})`, input)),
    body ? el("label", {}, tr("Body (JSON)"), body) : null,
    send,
    out,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    let url = path;
    const query = new URLSearchParams();
    for (const [p, input] of inputs) {
      const value = input.value.trim();
      if (!value) continue;
      if (p.in === "path") url = url.replace(`{${p.name}}`, encodeURIComponent(value));
      else query.append(p.name, value);
    }
    if (/\{[^}]+\}/.test(url)) return out.replaceChildren(el("p", { class: "error" }, tr("Fill in every path parameter.")));
    let payload;
    if (body) {
      try {
        payload = JSON.stringify(JSON.parse(body.value || "null"));
      } catch (e) {
        return out.replaceChildren(el("p", { class: "error" }, tr`The body is not JSON: ${e.message}`));
      }
    }
    if (query.toString()) url += `?${query}`;
    if (method !== "get" && !(await ask(tr`Send ${method.toUpperCase()} ${url}? It acts on real data, as your key.`, { yes: tr("Send") }))) return;
    await freshToken();
    const headers = withAuth();
    if (payload !== undefined) headers["Content-Type"] = "application/json";
    send.disabled = true;
    out.replaceChildren(el("p", { class: "muted" }, tr("Sending…")));
    const started = performance.now();
    try {
      const response = await fetch(url, { method: method.toUpperCase(), headers, body: payload });
      let text = await response.text();
      try {
        text = JSON.stringify(JSON.parse(text), null, 2);
      } catch {}
      const ms = Math.round(performance.now() - started);
      out.replaceChildren(
        el("p", { class: response.ok ? "muted" : "error" }, `${response.status} ${response.statusText} · ${ms} ms`),
        el("pre", {}, text.length > 20000 ? tr`${text.slice(0, 20000)}\n… (cut at 20,000 characters)` : text),
      );
    } catch (e) {
      out.replaceChildren(el("p", { class: "error" }, e.message));
    } finally {
      send.disabled = false;
    }
  });
  return el("details", { class: "try" }, el("summary", {}, tr("Try it")), form);
}

function operationView(path, method, op) {
  const params = op.parameters || [];
  const body = op.requestBody?.content?.["application/json"]?.schema;
  return el(
    "details",
    { class: "term" },
    el("summary", {}, el("code", { class: `method ${method}` }, method.toUpperCase()), " ", el("code", {}, path), " ", op.summary || ""),
    op.description ? el("p", { class: "doc" }, op.description) : null,
    params.length
      ? el(
          "div",
          {},
          el("h4", {}, tr("Parameters")),
          fieldsTable({
            properties: Object.fromEntries(params.map((p) => [`${p.name} (${p.in})`, { ...p.schema, description: p.description }])),
            required: params.filter((p) => p.required).map((p) => `${p.name} (${p.in})`),
          }, tr("Parameter")),
        )
      : null,
    body ? el("div", {}, el("h4", {}, tr`Body: ${typeOf(body)}`), fieldsTable(resolve(body))) : null,
    el("p", { class: "muted" }, tr`Responses: ${Object.keys(op.responses || {}).join(", ")}`),
    tryIt(path, method, op),
  );
}

function apiView(tag) {
  const ops = [];
  for (const [path, item] of Object.entries(docs.spec.paths)) {
    for (const method of METHODS) {
      const op = item[method];
      if (op && (op.tags?.[0] || "Other") === tag) ops.push(operationView(path, method, op));
    }
  }
  return [
    el("h2", {}, tag),
    el("p", { class: "muted" }, tr`Send the key in the ${KEY_HEADER} header. The machine-readable spec is /openapi.json. "Try it" sends a real request with the key above.`),
    ...ops,
  ];
}

function mcpConnectView() {
  const url = `${location.origin}${MCP_PATH}`;
  const config = {
    mcpServers: { truejoin: { type: "http", url, headers: { [KEY_HEADER]: tr("<your API key>") } } },
  };
  return [
    el("h2", {}, tr("Connect an MCP client")),
    el(
      "ul",
      { class: "plain" },
      el("li", {}, tr("Endpoint: "), el("code", {}, url)),
      el("li", {}, tr("Transport: Streamable HTTP, stateless, JSON responses (POST only).")),
      el("li", {}, tr("Key: the "), el("code", {}, KEY_HEADER), tr(" header; the key needs the query permission. Row rules and masks apply as they do in the console.")),
    ),
    el("h4", {}, tr("Client configuration")),
    el("pre", {}, JSON.stringify(config, null, 2)),
    el("h4", {}, tr("Over stdio, on the server's machine")),
    el("pre", {}, "truejoin serve --transport stdio -c config/database.yaml"),
    el("p", { class: "muted" }, tr("With auth on, mcp.stdio_api_key_name names the key a stdio session acts as.")),
  ];
}

function mcpToolsView() {
  if (docs.toolsError) return [el("h2", {}, tr("MCP tools")), el("p", { class: "error" }, docs.toolsError)];
  return [
    el("h2", {}, tr`MCP tools (${docs.tools.length})`),
    el("p", { class: "muted" }, tr("What this server's MCP endpoint offers (mcp.profile and mcp.tools decide), as a client sees it.")),
    ...docs.tools.map((tool) =>
      el(
        "details",
        { class: "term" },
        el("summary", {}, el("code", {}, tool.name)),
        el("p", { class: "doc" }, (tool.description || "").trim()),
        fieldsTable(tool.inputSchema || {}, tr("Argument")),
      ),
    ),
  ];
}

function renderDocs() {
  const pages = { "mcp-connect": tr("Connect"), "mcp-tools": tr("Tools") };
  const button = (page, label) =>
    el("a", { class: `item${docs.page === page ? " current" : ""}`, href: hashOf("docs", page), "aria-current": docs.page === page ? "page" : null }, label);
  const tags = docs.spec
    ? [...new Set(Object.values(docs.spec.paths).flatMap((item) => METHODS.filter((m) => item[m]).map((m) => item[m].tags?.[0] || "Other")))]
    : [];
  // The guide's chapters are the ones this reader may open; a chapter asked for
  // by an address they may not open gives way to the first.
  const chapters = guideChapters();
  if (docs.page.startsWith("guide-") && !chapters.some((c) => c.id === docs.page)) [docs.page, docs.part] = [chapters[0].id, null];
  const chapter = chapters.find((c) => c.id === docs.page);
  $("docs-nav").replaceChildren(
    el("h3", {}, tr("Guide")),
    ...chapters.flatMap((c) => [
      button(c.id, c.title),
      // The open chapter's sections, under it: each is a place with an address.
      ...(c === chapter ? c.sections.map((x) => el("a", { class: "item sub", href: hashOf("docs", c.id, x.id) }, x.title)) : []),
    ]),
    el("h3", {}, "MCP"),
    ...Object.entries(pages).map(([page, label]) => button(page, label)),
    el("h3", {}, "REST API"),
    ...(docs.spec ? tags.map((tag) => button(`api:${tag}`, tag)) : [el("p", { class: "error" }, docs.specError || tr("Loading…"))]),
  );
  let body;
  if (chapter) body = [el("div", { class: "guide" }, ...guideView(chapter))];
  else if (docs.page === "mcp-connect") body = mcpConnectView();
  else if (docs.page === "mcp-tools") body = docs.tools || docs.toolsError ? mcpToolsView() : [el("p", { class: "muted" }, tr("Loading…"))];
  else body = docs.spec ? apiView(docs.page.slice(4)) : [el("p", { class: "muted" }, tr("Pick a section."))];
  $("docs-body").replaceChildren(...body);
}

// After an address opened a page: the section it names is brought up and takes
// the focus (a screen reader then reads on from it); a page with none starts at its top.
function settleDocs() {
  const part = docs.part && document.getElementById(`${docs.page}-${docs.part}`);
  if (!part) return window.scrollTo(0, 0);
  part.focus({ preventScroll: true });
  part.scrollIntoView();
}

function openDoc(page) {
  docs.page = page;
  address("docs", [page]);
  renderDocs();
}

async function mcpTools() {
  await freshToken();
  const headers = withAuth({ "Content-Type": "application/json", Accept: "application/json, text/event-stream" });
  const response = await fetch(MCP_PATH, {
    method: "POST",
    headers,
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }),
  });
  if (response.status === 404) throw new Error(tr("MCP is not running on this server (it needs the OLAP engine)."));
  let data = {};
  try {
    data = await response.json();
  } catch {}
  if (!response.ok) throw new Error(data.detail || data.error?.message || response.statusText);
  if (data.error) throw new Error(data.error.message);
  return data.result.tools;
}

async function loadDocs() {
  docs.spec = docs.tools = docs.toolsError = docs.specError = null;
  renderDocs();
  await Promise.all([
    api("GET", "/openapi.json").then((s) => (docs.spec = s), (e) => (docs.specError = e.message)),
    // The tools are the query permission's: without it they are not asked for.
    ACCESS.query ? mcpTools().then((t) => (docs.tools = t), (e) => (docs.toolsError = e.message)) : (docs.toolsError = tr("This key cannot query.")),
  ]);
  renderDocs();
  if (docs.part) settleDocs(); // the page was redrawn: the section the address names is found again
}

// #docs, #docs/<page>, #docs/<chapter>/<section>: a chapter of the guide
// (guide-start, guide-query, …), mcp-connect, mcp-tools, api:<tag>
page("docs", {
  open([section, part]) {
    const was = docs.page;
    if (section) [docs.page, docs.part] = [section, part || null];
    else address("docs", [docs.page], true);
    if (!docs.spec && !docs.specError) loadDocs();
    else renderDocs();
    // A chapter this reader may not open gave way to the first: the address says so.
    if (section && docs.page !== section) address("docs", [docs.page], true);
    if (docs.part || docs.page !== was) settleDocs();
  },
  stale: () => (docs.spec = docs.specError = null),
});

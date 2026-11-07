"use strict";
// API and MCP reference, read live from this server: /openapi.json and the MCP
// endpoint's own tools/list. Uses el, api and $ from console.js.

const MCP_PATH = document.querySelector('meta[name="trio-mcp-path"]').content;
const docs = { spec: null, tools: null, toolsError: null, page: "mcp-connect" };
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
function fieldsTable(schema, nameHead = "Field") {
  const props = Object.entries(schema.properties || {});
  if (!props.length) return el("p", { class: "muted" }, "No fields.");
  const required = new Set(schema.required || []);
  return el(
    "div",
    { class: "scroll" },
    el(
      "table",
      { class: "review" },
      el("thead", {}, el("tr", {}, ...[nameHead, "Type", "Required", "Description"].map((h) => el("th", { scope: "col" }, h)))),
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
  const body = schema ? el("textarea", { class: "field", rows: "6", "aria-label": "Body (JSON)", spellcheck: "false" }) : null;
  if (body) body.value = JSON.stringify(sample(schema), null, 2);
  const out = el("div", { "aria-live": "polite" });
  const send = el("button", { type: "submit" }, "Send");
  const form = el(
    "form",
    { class: "analyze" },
    ...inputs.map(([p, input]) => el("label", {}, `${p.name} (${p.in})`, input)),
    body ? el("label", {}, "Body (JSON)", body) : null,
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
    if (/\{[^}]+\}/.test(url)) return out.replaceChildren(el("p", { class: "error" }, "Fill in every path parameter."));
    let payload;
    if (body) {
      try {
        payload = JSON.stringify(JSON.parse(body.value || "null"));
      } catch (e) {
        return out.replaceChildren(el("p", { class: "error" }, `The body is not JSON: ${e.message}`));
      }
    }
    if (query.toString()) url += `?${query}`;
    if (method !== "get" && !confirm(`Send ${method.toUpperCase()} ${url}? It acts on real data, as your key.`)) return;
    const headers = withAuth();
    if (payload !== undefined) headers["Content-Type"] = "application/json";
    send.disabled = true;
    out.replaceChildren(el("p", { class: "muted" }, "Sending…"));
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
        el("pre", {}, text.length > 20000 ? `${text.slice(0, 20000)}\n… (cut at 20,000 characters)` : text),
      );
    } catch (e) {
      out.replaceChildren(el("p", { class: "error" }, e.message));
    } finally {
      send.disabled = false;
    }
  });
  return el("details", { class: "try" }, el("summary", {}, "Try it"), form);
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
          el("h4", {}, "Parameters"),
          fieldsTable({
            properties: Object.fromEntries(params.map((p) => [`${p.name} (${p.in})`, { ...p.schema, description: p.description }])),
            required: params.filter((p) => p.required).map((p) => `${p.name} (${p.in})`),
          }, "Parameter"),
        )
      : null,
    body ? el("div", {}, el("h4", {}, `Body: ${typeOf(body)}`), fieldsTable(resolve(body))) : null,
    el("p", { class: "muted" }, `Responses: ${Object.keys(op.responses || {}).join(", ")}`),
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
    el("p", { class: "muted" }, `Send the key in the ${KEY_HEADER} header. The machine-readable spec is /openapi.json. "Try it" sends a real request with the key above.`),
    ...ops,
  ];
}

function mcpConnectView() {
  const url = `${location.origin}${MCP_PATH}`;
  const config = {
    mcpServers: { trio: { type: "http", url, headers: { [KEY_HEADER]: "<your API key>" } } },
  };
  return [
    el("h2", {}, "Connect an MCP client"),
    el(
      "ul",
      { class: "plain" },
      el("li", {}, "Endpoint: ", el("code", {}, url)),
      el("li", {}, "Transport: Streamable HTTP, stateless, JSON responses (POST only)."),
      el("li", {}, "Key: the ", el("code", {}, KEY_HEADER), " header; the key needs the query permission. Row rules and masks apply as they do in the console."),
    ),
    el("h4", {}, "Client configuration"),
    el("pre", {}, JSON.stringify(config, null, 2)),
    el("h4", {}, "Over stdio, on the server's machine"),
    el("pre", {}, "trio serve --transport stdio -c config/database.yaml"),
    el("p", { class: "muted" }, "With auth on, mcp.stdio_api_key_name names the key a stdio session acts as."),
  ];
}

function mcpToolsView() {
  if (docs.toolsError) return [el("h2", {}, "MCP tools"), el("p", { class: "error" }, docs.toolsError)];
  return [
    el("h2", {}, `MCP tools (${docs.tools.length})`),
    el("p", { class: "muted" }, "What this server's MCP endpoint offers (mcp.profile and mcp.tools decide), as a client sees it."),
    ...docs.tools.map((tool) =>
      el(
        "details",
        { class: "term" },
        el("summary", {}, el("code", {}, tool.name)),
        el("p", { class: "doc" }, (tool.description || "").trim()),
        fieldsTable(tool.inputSchema || {}, "Argument"),
      ),
    ),
  ];
}

function renderDocs() {
  const pages = { "mcp-connect": "Connect", "mcp-tools": "Tools" };
  const button = (page, label) =>
    el("button", { type: "button", class: `item${docs.page === page ? " current" : ""}`, onclick: () => openDoc(page) }, label);
  const tags = docs.spec
    ? [...new Set(Object.values(docs.spec.paths).flatMap((item) => METHODS.filter((m) => item[m]).map((m) => item[m].tags?.[0] || "Other")))]
    : [];
  $("docs-nav").replaceChildren(
    el("h3", {}, "MCP"),
    ...Object.entries(pages).map(([page, label]) => button(page, label)),
    el("h3", {}, "REST API"),
    ...(docs.spec ? tags.map((tag) => button(`api:${tag}`, tag)) : [el("p", { class: "error" }, docs.specError || "Loading…")]),
  );
  let body;
  if (docs.page === "mcp-connect") body = mcpConnectView();
  else if (docs.page === "mcp-tools") body = docs.tools || docs.toolsError ? mcpToolsView() : [el("p", { class: "muted" }, "Loading…")];
  else body = docs.spec ? apiView(docs.page.slice(4)) : [el("p", { class: "muted" }, "Pick a section.")];
  $("docs-body").replaceChildren(...body);
}

function openDoc(page) {
  docs.page = page;
  renderDocs();
}

async function mcpTools() {
  const headers = withAuth({ "Content-Type": "application/json", Accept: "application/json, text/event-stream" });
  const response = await fetch(MCP_PATH, {
    method: "POST",
    headers,
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }),
  });
  if (response.status === 404) throw new Error("MCP is not running on this server (it needs the OLAP engine).");
  let data = {};
  try {
    data = await response.json();
  } catch {}
  if (!response.ok) throw new Error(`${response.status}: ${data.detail || data.error?.message || response.statusText}`);
  if (data.error) throw new Error(data.error.message);
  return data.result.tools;
}

async function loadDocs() {
  docs.spec = docs.tools = docs.toolsError = docs.specError = null;
  renderDocs();
  await Promise.all([
    api("GET", "/openapi.json").then((s) => (docs.spec = s), (e) => (docs.specError = e.message)),
    mcpTools().then((t) => (docs.tools = t), (e) => (docs.toolsError = e.message)),
  ]);
  renderDocs();
}

$("tab-docs").addEventListener("click", () => {
  if (!docs.spec && !docs.specError) loadDocs();
});
$("auth").addEventListener("submit", () => {
  if (!$("view-docs").hidden) loadDocs();
});

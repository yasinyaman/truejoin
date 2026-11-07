"use strict";
// The whole catalog at once: what analysis found and reviewers kept, read-only.
// Editing stays in the table review. Uses el, api, $ (console.js) and review,
// badge, verdict, catalogPath, openTable (catalog.js).

const SVG = "http://www.w3.org/2000/svg";

function svg(tag, attrs = {}, ...children) {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  e.append(...children.filter((c) => c !== null && c !== undefined));
  return e;
}

const ltext = (localized) => localized?.texts?.[review.lang] || "";
const list = (items) => (items && items.length ? items.join(", ") : "");

async function openOntology() {
  review.table = null;
  renderNav();
  const target = $("catalog-table");
  target.replaceChildren(el("p", { class: "muted" }, "Loading…"));
  try {
    const catalog = await api("GET", catalogPath(review.name, "ontology"));
    target.replaceChildren(...ontologyView(catalog).filter(Boolean));
  } catch (e) {
    target.replaceChildren(el("p", { class: "error" }, e.message));
  }
}

function ontologyView(c) {
  const tables = Object.values(c.tables);
  const columns = tables.flatMap((t) => t.columns);
  const rels = tables.flatMap((t) => t.relationships);
  const terms = Object.values(c.terms || {});
  const measures = terms.flatMap((t) => t.sql_attributes.map((m) => ({ ...m, term: t.name })));
  const count = (items, test) => `${items.filter(test).length} of ${items.length}`;
  return [
    el("h3", {}, "Ontology"),
    ltext(c.description) ? el("p", {}, ltext(c.description)) : null,
    el(
      "dl",
      { class: "facts" },
      ...fact("Database", `${c.database_name} (${c.database_type})`),
      ...fact("Status", c.status),
      ...fact("Languages", list(c.languages)),
      ...fact("Written by", c.llm_model ? `${c.llm_provider}: ${c.llm_model}` : "not recorded"),
      ...fact("Generated", c.generated_at ? new Date(c.generated_at).toLocaleString() : ""),
      ...fact("Updated", c.updated_at ? new Date(c.updated_at).toLocaleString() : ""),
      ...fact("Tables", String(tables.length)),
      ...fact("Columns", `${columns.length}, ${columns.filter((x) => x.personal).length} personal`),
      ...fact("Relationships", `${count(rels, (r) => r.verified === true)} verified by the data`),
      ...fact("Terms", `${count(terms, (t) => t.certified)} certified`),
      ...fact("Measures", `${count(measures, (m) => m.certified)} certified`),
    ),
    guidanceView(c),
    tagsView(c),
    el("h4", {}, "Entities and relationships"),
    diagram(tables),
    el("h4", {}, "Entities"),
    ...tables.map((t) => entityView({ ...t, owner: (c.table_owners || {})[t.table_name] })),
    el("h4", {}, "Glossary"),
    terms.length ? null : el("p", { class: "muted" }, "No business terms yet."),
    ...terms.map((t) => glossaryEntry({ ...t, owner: (c.term_owners || {})[t.name] })),
    el("h4", {}, "Measures"),
    measures.length ? measuresTable(measures) : el("p", { class: "muted" }, "No expressions yet: `trio catalog suggest` mines them from query history."),
  ];
}

// Abbreviations are written out before a question is matched; notes reach a model with the source.
// One "short = long" per line, and notes split by an empty line, so both edit as plain text.
function guidanceView(c) {
  const glossary = Object.entries(c.glossary || {});
  const notes = c.notes || [];
  const note = el("p", { class: "muted" }, c.status === "approved" ? "In use now." : "Used once the catalog is approved.");
  if (!review.canEdit) {
    return el(
      "section",
      {},
      el("h4", {}, "Abbreviations and notes for the model"),
      glossary.length ? el("ul", { class: "plain" }, ...glossary.map(([s, l]) => el("li", {}, el("code", {}, s), ` = ${l}`))) : el("p", { class: "muted" }, "No abbreviations."),
      ...(notes.length ? notes.map((n) => el("p", { class: "doc" }, n)) : [el("p", { class: "muted" }, "No notes.")]),
    );
  }
  const words = el("textarea", { class: "field", rows: "4", spellcheck: "false", "aria-label": "Abbreviations", placeholder: "SP = sipariş\nKDV dahil = brüt tutar" });
  words.value = glossary.map(([s, l]) => `${s} = ${l}`).join("\n");
  const written = el("textarea", { class: "field", rows: "4", "aria-label": "Notes for the model", placeholder: "Amounts are in TRY.\n\nA cancelled (iptal) transaction is not revenue." });
  written.value = notes.join("\n\n");
  const verdict = el("p", { class: "muted", "aria-live": "polite" });
  const form = el(
    "form",
    { class: "analyze" },
    el("label", {}, "Abbreviations: one per line, short = what it stands for", words),
    el("label", {}, "Notes: what a model should know about this data, separated by an empty line", written),
    el("button", { type: "submit" }, "Save"),
    verdict,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const pairs = words.value.split("\n").map((line) => line.trim()).filter(Boolean);
    const bad = pairs.find((line) => !line.includes("="));
    if (bad) {
      verdict.className = "error";
      verdict.textContent = `"${bad}" has no "=": write it as short = what it stands for.`;
      return;
    }
    const body = {
      glossary: Object.fromEntries(pairs.map((line) => [line.split("=")[0].trim(), line.slice(line.indexOf("=") + 1).trim()])),
      notes: written.value.split(/\n\s*\n/).map((n) => n.trim()).filter(Boolean),
    };
    const saved = await act("Saving the abbreviations and notes", () => api("PATCH", catalogPath(review.name, "guidance"), body));
    if (saved) {
      words.value = Object.entries(saved.glossary).map(([s, l]) => `${s} = ${l}`).join("\n");
      written.value = saved.notes.join("\n\n");
      verdict.className = "muted";
      verdict.textContent = `Saved ${Object.keys(saved.glossary).length} abbreviation(s) and ${saved.notes.length} note(s).`;
    }
  });
  return el(
    "section",
    {},
    el("h4", {}, "Abbreviations and notes for the model"),
    el("p", { class: "muted" }, "answer_question and search_terms write the abbreviations out before matching a question; list_sources and the OpenAPI context give a model the notes."),
    note,
    form,
  );
}

// Every tag and what carries it; renamed or removed across the catalog (drafts only, as other edits).
function tagsView(c) {
  const found = new Map();
  const add = (tag, where) => {
    if (!found.has(tag)) found.set(tag, []);
    found.get(tag).push(where);
  };
  for (const t of Object.values(c.tables)) {
    for (const tag of t.tags || []) add(tag, t.table_name);
    for (const col of t.columns) for (const tag of col.tags || []) add(tag, `${t.table_name}.${col.name}`);
  }
  const editable = review.canEdit && c.status === "draft";
  const tags = [...found.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  const path = (tag) => catalogPath(review.name, "tags", tag);
  const rename = async (tag) => {
    const name = prompt(`Rename the tag "${tag}" everywhere to:`, tag);
    if (name && name.trim() && name.trim() !== tag && (await act(`Renaming ${tag}`, () => api("PATCH", path(tag), { name: name.trim() })))) {
      await openCatalog(review.name);
    }
  };
  const remove = async (tag, n) => {
    if (confirm(`Take the tag "${tag}" off all ${n} table(s) and column(s)?`) && (await act(`Removing ${tag}`, () => api("DELETE", path(tag))))) {
      await openCatalog(review.name);
    }
  };
  return el(
    "section",
    {},
    el("h4", {}, "Tags"),
    tags.length
      ? el(
          "ul",
          { class: "plain" },
          ...tags.map(([tag, where]) =>
            el(
              "li",
              {},
              el("span", { class: "badge" }, tag),
              " ",
              el("span", { class: "muted" }, where.join(", ")),
              editable ? el("button", { type: "button", class: "link", onclick: () => rename(tag) }, " rename") : null,
              editable ? el("button", { type: "button", class: "link", onclick: () => remove(tag, where.length) }, " remove") : null,
            ),
          ),
        )
      : el("p", { class: "muted" }, "No tags yet."),
    review.canEdit && !editable
      ? el("p", { class: "muted" }, "Re-open the catalog for review to rename or remove tags: a tag can make a column personal.")
      : null,
  );
}

function fact(name, value) {
  return [el("dt", {}, name), el("dd", {}, value || "—")];
}

// Tables on a circle, edges clipped to the boxes. Click a box to review it.
function diagram(tables) {
  if (tables.length > 40) return el("p", { class: "muted" }, `${tables.length} tables: too many to draw; see the list below.`);
  const W = 150, H = 44;
  const n = tables.length;
  const r = n < 2 ? 0 : Math.max(110, (n * (W + 30)) / (2 * Math.PI));
  const cx = r + W / 2 + 10, cy = r + H / 2 + 10;
  const pos = {};
  tables.forEach((t, i) => {
    const a = (2 * Math.PI * i) / n - Math.PI / 2;
    pos[t.table_name] = { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) };
  });
  const clip = (from, to) => {
    const dx = to.x - from.x, dy = to.y - from.y;
    const k = Math.min(dx ? W / 2 / Math.abs(dx) : Infinity, dy ? H / 2 / Math.abs(dy) : Infinity);
    return { x: from.x + dx * k, y: from.y + dy * k };
  };
  const edges = [];
  const seen = new Set();
  for (const t of tables) {
    const link = (from, to, toColumn, extra) => ({
      to,
      // Either direction of one column pair is one edge; relationships come first, so a
      // declared key they already state is not drawn twice.
      key: [`${t.table_name}.${from}`, `${to}.${toColumn}`].sort().join("|"),
      label: `${t.table_name}.${from} → ${to}.${toColumn}`,
      ...extra,
    });
    const links = [
      ...t.relationships.map((rel) => link(rel.source_column, rel.target_table, rel.target_column, { verified: rel.verified, kind: rel.relationship_type })),
      ...t.foreign_keys.map((fk) => link(fk.column, fk.references_table, fk.references_column, { declared: true })),
    ];
    for (const link of links) {
      if (seen.has(link.key) || !pos[link.to] || link.to === t.table_name) continue;
      seen.add(link.key);
      const a = clip(pos[t.table_name], pos[link.to]);
      const b = clip(pos[link.to], pos[t.table_name]);
      const cls = link.declared ? "edge declared" : link.verified === true ? "edge ok" : link.verified === false ? "edge bad" : "edge unchecked";
      edges.push(
        svg(
          "line",
          { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: cls, "marker-end": "url(#arrow)" },
          svg("title", {}, `${link.label}${link.kind ? ` (${link.kind})` : ""}`),
        ),
      );
    }
  }
  const boxes = tables.map((t) => {
    const p = pos[t.table_name];
    const personal = t.columns.filter((x) => x.personal).length;
    return svg(
      "g",
      { class: "entity", tabindex: "0", role: "button", onclick: () => openTable(t.table_name), onkeydown: (e) => e.key === "Enter" && openTable(t.table_name) },
      svg("title", {}, `${t.table_name}: ${ltext(t.description)}`),
      svg("rect", { x: p.x - W / 2, y: p.y - H / 2, width: W, height: H, rx: 6 }),
      svg("text", { x: p.x, y: p.y - 4, "text-anchor": "middle", class: "name" }, t.table_name),
      svg("text", { x: p.x, y: p.y + 12, "text-anchor": "middle", class: "sub" }, `${t.columns.length} cols${personal ? ` · ${personal} personal` : ""}`),
    );
  });
  const width = 2 * (r + W / 2 + 10), height = 2 * (r + H / 2 + 10);
  return el(
    "figure",
    { class: "diagram" },
    // Drawn at its own size, shrunk to fit a narrow screen but never blown up.
    svg(
      "svg",
      { viewBox: `0 0 ${width} ${height}`, width, height, role: "img", "aria-label": "Tables and their relationships" },
      svg("defs", {}, svg("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" }, svg("path", { d: "M0,0 L10,5 L0,10 z", class: "arrowhead" }))),
      ...edges,
      ...boxes,
    ),
    el(
      "figcaption",
      { class: "muted legend" },
      el("span", { class: "key ok" }, "verified by the data"),
      el("span", { class: "key unchecked" }, "not checked"),
      el("span", { class: "key bad" }, "contradicted"),
      el("span", { class: "key declared" }, "declared key only"),
      " Only verified relationships and declared keys are used for joins. Click a table to review it.",
    ),
  );
}

function entityView(t) {
  const pk = Array.isArray(t.primary_key) ? t.primary_key.join(", ") : t.primary_key;
  return el(
    "details",
    { class: "term" },
    el(
      "summary",
      {},
      el("strong", {}, t.table_name),
      ltext(t.human_name) ? ` · ${ltext(t.human_name)}` : "",
      " ",
      badge(t.review_status),
      el("span", { class: "muted" }, ` ${t.columns.length} columns${t.row_count != null && t.row_count >= 0 ? ` · ~${t.row_count.toLocaleString()} rows` : ""}`),
    ),
    ltext(t.description) ? el("p", {}, ltext(t.description)) : null,
    t.owner ? el("p", { class: "muted" }, `Owner: ${t.owner}`) : null,
    el(
      "dl",
      { class: "facts" },
      ...fact("Primary key", pk),
      ...fact("Foreign keys", list(t.foreign_keys.map((fk) => `${fk.column} → ${fk.references_table}.${fk.references_column}`))),
      ...fact("Indexes", list(t.indexes.map((i) => `${i.name} (${i.columns.join(", ")})${i.unique ? " unique" : ""}`))),
      ...fact("Tags", list(t.tags)),
      ...fact("DB comment", t.db_comment),
    ),
    el(
      "div",
      { class: "scroll" },
      el(
        "table",
        { class: "review" },
        el("thead", {}, el("tr", {}, ...["Column", "Type", "Null", "Default", "Semantic type", "Tags", "Description", "DB comment", "Samples"].map((h) => el("th", { scope: "col" }, h)))),
        el(
          "tbody",
          {},
          ...t.columns.map((col) =>
            el(
              "tr",
              {},
              el(
                "td",
                {},
                el("code", {}, col.name),
                col.is_primary_key ? el("span", { class: "badge" }, "key") : null,
                col.references ? el("span", { class: "badge" }, `→ ${col.references}`) : null,
                col.personal ? el("span", { class: "badge status-modified" }, "personal") : null,
                col.masked ? el("span", { class: "badge" }, "masked for you") : null,
              ),
              el("td", { class: "muted" }, col.full_type || col.data_type),
              el("td", {}, col.nullable ? "yes" : "no"),
              el("td", { class: "muted" }, col.default_value || ""),
              el("td", {}, col.semantic_type || ""),
              el("td", {}, list(col.tags)),
              el("td", {}, ltext(col.description)),
              el("td", { class: "muted" }, col.db_comment || ""),
              el("td", { class: "muted" }, (col.sample_values || []).map(String).join(", ")),
            ),
          ),
        ),
      ),
    ),
    t.relationships.length
      ? el(
          "ul",
          { class: "plain" },
          ...t.relationships.map((rel) =>
            el(
              "li",
              {},
              `${rel.source_column} → ${rel.target_table}.${rel.target_column} `,
              el("span", { class: "muted" }, rel.relationship_type),
              verdict(rel.verified),
              ltext(rel.description) ? el("span", { class: "muted" }, ` ${ltext(rel.description)}`) : null,
            ),
          ),
        )
      : null,
  );
}

function glossaryEntry(term) {
  return el(
    "details",
    { class: "term" },
    el(
      "summary",
      {},
      el("strong", {}, term.name),
      " ",
      term.certified ? el("span", { class: "badge status-approved" }, "certified") : el("span", { class: "badge status-pending" }, "not certified"),
      el("span", { class: "muted" }, ` ${list(term.tables)}`),
    ),
    ltext(term.description) ? el("p", {}, ltext(term.description)) : null,
    term.owner ? el("p", { class: "muted" }, `Owner: ${term.owner}`) : null,
    el("dl", { class: "facts" }, ...fact("Synonyms", list(term.synonyms))),
    term.column_attributes.length
      ? el(
          "div",
          { class: "scroll" },
          el(
            "table",
            { class: "review" },
            el("thead", {}, el("tr", {}, ...["Column", "Words", "Values"].map((h) => el("th", { scope: "col" }, h)))),
            el(
              "tbody",
              {},
              ...term.column_attributes.map((a) =>
                el(
                  "tr",
                  {},
                  el("td", {}, el("code", {}, `${a.table}.${a.column}`)),
                  el("td", {}, list(a.synonyms)),
                  el("td", { class: "muted" }, Object.entries(a.values).map(([v, ws]) => (ws.length ? `${v} (${ws.join(", ")})` : v)).join("; ")),
                ),
              ),
            ),
          ),
        )
      : null,
  );
}

function measuresTable(measures) {
  return el(
    "div",
    { class: "scroll" },
    el(
      "table",
      { class: "review" },
      el("thead", {}, el("tr", {}, ...["Measure", "Table", "Expression", "Description", "Term", "From queries", "Status"].map((h) => el("th", { scope: "col" }, h)))),
      el(
        "tbody",
        {},
        ...measures.map((m) =>
          el(
            "tr",
            {},
            el("td", {}, el("code", {}, m.name)),
            el("td", {}, m.table),
            el("td", {}, el("code", {}, m.expression)),
            el("td", {}, ltext(m.description)),
            el("td", {}, m.term),
            el("td", { class: "muted" }, String(m.source_sql_ids.length)),
            el("td", {}, m.certified ? el("span", { class: "badge status-approved" }, "measure") : el("span", { class: "badge" }, "draft")),
          ),
        ),
      ),
    ),
  );
}

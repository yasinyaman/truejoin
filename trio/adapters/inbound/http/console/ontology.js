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
    examplesView(c),
    tagsView(c),
    el("h4", {}, "Entities and relationships"),
    diagram(tables, terms),
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

// Saved analyses: questions and the SQL that answers them; find_examples offers the certified ones to models.
function examplesView(c) {
  const examples = Object.values(c.examples || {});
  const path = (...parts) => catalogPath(review.name, "examples", ...parts);
  const reload = () => openOntology();
  const certify = async (x, certified) => {
    if (await act(`${certified ? "Certifying" : "Withdrawing"} ${x.name}`, () => api("PATCH", path(x.name), { certified }))) await reload();
  };
  const remove = async (x) => {
    if (confirm(`Remove the example ${x.name}?`) && (await act(`Removing ${x.name}`, () => api("DELETE", path(x.name))))) await reload();
  };
  const card = (x) =>
    el(
      "details",
      { class: "term" },
      el(
        "summary",
        {},
        el("strong", {}, ltext(x.question) || x.name),
        " ",
        x.certified ? el("span", { class: "badge status-approved" }, "certified") : el("span", { class: "badge status-pending" }, "draft"),
        el("span", { class: "muted" }, ` ${x.name} · ${list(x.tables)}`),
      ),
      ltext(x.description) ? el("p", {}, ltext(x.description)) : null,
      el("pre", {}, x.sql),
      review.canEdit
        ? el(
            "div",
            { class: "bar" },
            el("button", { type: "button", class: "secondary", onclick: () => certify(x, !x.certified) }, x.certified ? "Withdraw certification" : "Certify"),
            el("button", { type: "button", class: "secondary", onclick: () => remove(x) }, "Remove"),
          )
        : null,
    );
  return el(
    "section",
    {},
    el("h4", {}, "Saved analyses"),
    el("p", { class: "muted" }, "Questions and the SQL that answers them here. Certified ones are what find_examples offers a model before it writes SQL; one quoting a value is never shown to a caller with row rules or masks."),
    ...(examples.length ? examples.map(card) : [el("p", { class: "muted" }, "None yet.")]),
    review.canEdit ? exampleForm(c) : null,
  );
}

function exampleForm(c) {
  const first = Object.keys(c.tables)[0] || "table";
  const name = el("input", { class: "field", "aria-label": "Example name", placeholder: "paid_per_branch" });
  const question = el("input", { class: "field", "aria-label": "Question", placeholder: "Şube bazında ödenen tutar nedir?" });
  const sql = el("textarea", { class: "field", rows: "4", spellcheck: "false", "aria-label": "SQL", placeholder: `SELECT ... FROM ${c.database_name}.${first} ...` });
  const description = el("input", { class: "field", "aria-label": "Example description", placeholder: "what it shows, if the question does not say" });
  const verdict = el("div", { "aria-live": "polite" });
  const show = (text, kind = "muted") => verdict.replaceChildren(el("p", { class: kind }, text));
  const tryIt = el("button", { type: "button", class: "secondary" }, "Run it");
  tryIt.addEventListener("click", async () => {
    try {
      const r = await api("POST", "/olap/query", { sql: sql.value });
      show(`Runs: ${r.row_count} row(s); columns ${(r.columns || []).join(", ")}.`);
    } catch (e) {
      show(e.message, "error");
    }
  });
  const form = el(
    "form",
    { class: "analyze" },
    el("label", {}, "Name", name),
    el("label", {}, "Question", question),
    el("label", {}, `SQL, with tables named as ${c.database_name}.table`, sql),
    el("label", {}, "Description", description),
    el("div", { class: "bar" }, tryIt, el("button", { type: "submit" }, "Save as draft")),
    verdict,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("POST", catalogPath(review.name, "examples"), {
        name: name.value, question: question.value, sql: sql.value, description: description.value, lang: review.lang,
      });
      await openOntology();
      say(`Saved ${name.value} as a draft: certify it to offer it to models.`);
    } catch (e) {
      show(e.message, "error");
    }
  });
  return el("details", { class: "try" }, el("summary", {}, "Save an analysis"), form);
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

// Tables on a circle to start. Drag a table to move it, drag the background to
// pan, Ctrl+wheel (a trackpad pinch) or the buttons to zoom. Selecting a table
// lights it and its neighbours and shows below what it means and how it joins.
function diagram(tables, terms = []) {
  if (tables.length > 80) return el("p", { class: "muted" }, `${tables.length} tables: too many to draw; see the list below.`);
  const W = 150, H = 44;
  const n = tables.length;
  const r = n < 2 ? 0 : Math.max(110, (n * (W + 30)) / (2 * Math.PI));
  const cx = r + W / 2 + 10, cy = r + H / 2 + 10;
  const home = {};
  tables.forEach((t, i) => {
    const a = (2 * Math.PI * i) / n - Math.PI / 2;
    home[t.table_name] = { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) };
  });
  const pos = Object.fromEntries(Object.entries(home).map(([k, p]) => [k, { ...p }]));
  const byName = Object.fromEntries(tables.map((t) => [t.table_name, t]));
  const clip = (from, to) => {
    const dx = to.x - from.x, dy = to.y - from.y;
    const k = Math.min(dx ? W / 2 / Math.abs(dx) : Infinity, dy ? H / 2 / Math.abs(dy) : Infinity);
    return { x: from.x + dx * k, y: from.y + dy * k };
  };

  const edges = [];
  const seen = new Set();
  for (const t of tables) {
    const link = (from, to, toColumn, extra) => ({
      from: t.table_name,
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
    for (const e of links) {
      if (seen.has(e.key) || !pos[e.to] || e.to === t.table_name) continue;
      seen.add(e.key);
      e.cls = e.declared ? "edge declared" : e.verified === true ? "edge ok" : e.verified === false ? "edge bad" : "edge unchecked";
      e.line = svg("line", { class: e.cls, "marker-end": "url(#arrow)" }, svg("title", {}, `${e.label}${e.kind ? ` (${e.kind})` : ""}`));
      edges.push(e);
    }
  }
  const neighbours = (name) => new Set(edges.flatMap((e) => (e.from === name ? [e.to] : e.to === name ? [e.from] : [])));

  const nodes = {};
  for (const t of tables) {
    const personal = t.columns.filter((x) => x.personal).length;
    nodes[t.table_name] = svg(
      "g",
      { class: "entity", tabindex: "0", role: "button", "aria-label": `${t.table_name}: show what it means`, "data-table": t.table_name },
      svg("title", {}, `${t.table_name}: ${ltext(t.description)}`),
      svg("rect", { x: -W / 2, y: -H / 2, width: W, height: H, rx: 6 }),
      svg("text", { x: 0, y: -4, "text-anchor": "middle", class: "name" }, t.table_name),
      svg("text", { x: 0, y: 12, "text-anchor": "middle", class: "sub" }, `${t.columns.length} cols${personal ? ` · ${personal} personal` : ""}`),
    );
  }
  const place = (name) => {
    nodes[name].setAttribute("transform", `translate(${pos[name].x} ${pos[name].y})`);
    for (const e of edges) {
      if (e.from !== name && e.to !== name) continue;
      const a = clip(pos[e.from], pos[e.to]);
      const b = clip(pos[e.to], pos[e.from]);
      for (const [k, v] of Object.entries({ x1: a.x, y1: a.y, x2: b.x, y2: b.y })) e.line.setAttribute(k, v);
    }
  };

  const width = 2 * (r + W / 2 + 10), height = 2 * (r + H / 2 + 10);
  const full = { x: 0, y: 0, w: width, h: height };
  let vb = { ...full };
  const root = svg(
    "svg",
    { viewBox: `0 0 ${width} ${height}`, width: "100%", height: Math.min(height, 520), role: "group", "aria-label": "Tables and their relationships", class: "graph" },
    svg("defs", {}, svg("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" }, svg("path", { d: "M0,0 L10,5 L0,10 z", class: "arrowhead" }))),
    ...edges.map((e) => e.line),
    ...Object.values(nodes),
  );
  const show = () => root.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  Object.keys(nodes).forEach(place);

  // Client pixels per drawing unit, as preserveAspectRatio "meet" draws it.
  const scale = () => {
    const box = root.getBoundingClientRect();
    return Math.min(box.width / vb.w, box.height / vb.h) || 1;
  };
  const zoom = (factor, at = { x: vb.x + vb.w / 2, y: vb.y + vb.h / 2 }) => {
    const w = Math.min(Math.max(vb.w * factor, full.w / 8), full.w * 4);
    const f = w / vb.w;
    vb = { x: at.x - (at.x - vb.x) * f, y: at.y - (at.y - vb.y) * f, w, h: vb.h * f };
    show();
  };

  const detail = el("div", { class: "graph-detail", "aria-live": "polite" });
  const select = (name) => {
    const near = name ? neighbours(name) : new Set();
    for (const [k, g] of Object.entries(nodes)) {
      g.classList.toggle("selected", k === name);
      g.classList.toggle("dim", Boolean(name) && k !== name && !near.has(k));
    }
    for (const e of edges) e.line.classList.toggle("dim", Boolean(name) && e.from !== name && e.to !== name);
    detail.replaceChildren(...(name ? tableCard(byName[name], terms, edges, select) : []));
  };

  // One pointer at a time: on a table it moves the table, elsewhere it pans; a press that
  // does not move is a click, which selects (or, on the background, clears).
  let drag = null;
  root.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const g = e.target.closest(".entity");
    const name = g ? g.dataset.table : null;
    drag = { name, x: e.clientX, y: e.clientY, from: name ? { ...pos[name] } : { ...vb }, moved: false, id: e.pointerId };
    root.setPointerCapture(e.pointerId);
  });
  root.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
    drag.moved = true;
    root.classList.add("dragging");
    const s = scale();
    if (drag.name) {
      pos[drag.name] = { x: drag.from.x + dx / s, y: drag.from.y + dy / s };
      place(drag.name);
    } else {
      vb = { ...vb, x: drag.from.x - dx / s, y: drag.from.y - dy / s };
      show();
    }
  });
  const release = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.moved) select(drag.name);
    root.classList.remove("dragging");
    drag = null;
  };
  root.addEventListener("pointerup", release);
  root.addEventListener("pointercancel", release);
  // Ctrl+wheel only: a plain wheel keeps scrolling the page. A trackpad pinch arrives as Ctrl+wheel.
  root.addEventListener("wheel", (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    const box = root.getBoundingClientRect();
    const s = scale();
    const at = { x: vb.x + (e.clientX - box.left - (box.width - vb.w * s) / 2) / s, y: vb.y + (e.clientY - box.top - (box.height - vb.h * s) / 2) / s };
    zoom(e.deltaY > 0 ? 1.15 : 1 / 1.15, at);
  });
  root.addEventListener("keydown", (e) => {
    const g = e.target.closest(".entity");
    if (g && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      select(g.dataset.table);
    } else if (e.key === "Escape") select(null);
  });

  const button = (label, onclick) => el("button", { type: "button", class: "secondary", onclick }, label);
  return el(
    "figure",
    { class: "diagram" },
    el(
      "div",
      { class: "bar" },
      button("Zoom in", () => zoom(1 / 1.3)),
      button("Zoom out", () => zoom(1.3)),
      button("Fit", () => {
        vb = { ...full };
        show();
      }),
      button("Reset layout", () => {
        for (const [k, p] of Object.entries(home)) pos[k] = { ...p };
        Object.keys(nodes).forEach(place);
        vb = { ...full };
        show();
      }),
    ),
    root,
    el(
      "figcaption",
      { class: "muted legend" },
      el("span", { class: "key ok" }, "verified by the data"),
      el("span", { class: "key unchecked" }, "not checked"),
      el("span", { class: "key bad" }, "contradicted"),
      el("span", { class: "key declared" }, "declared key only"),
      " Only verified relationships and declared keys are used for joins. Drag a table to move it, the background to pan; Ctrl+wheel to zoom. Select a table to see what it means.",
    ),
    detail,
  );
}

// What a selected table means: its terms and measures, and what it joins to (a neighbour is one click away).
function tableCard(t, terms, edges, select) {
  const name = t.table_name;
  const mine = terms.filter((term) => (term.tables || []).includes(name));
  const measures = mine.flatMap((term) => term.sql_attributes.filter((m) => m.table === name).map((m) => ({ ...m, term: term.name })));
  const joins = edges.filter((e) => e.from === name || e.to === name);
  const pk = Array.isArray(t.primary_key) ? t.primary_key.join(", ") : t.primary_key;
  return [
    el(
      "div",
      { class: "bar" },
      el("h4", {}, name, ltext(t.human_name) ? ` · ${ltext(t.human_name)}` : ""),
      badge(t.review_status),
      el("button", { type: "button", onclick: () => openTable(name) }, "Review this table"),
      el("button", { type: "button", class: "link", onclick: () => select(null) }, "Clear"),
    ),
    ltext(t.description) ? el("p", {}, ltext(t.description)) : null,
    el(
      "dl",
      { class: "facts" },
      ...fact("Columns", `${t.columns.length}${t.row_count != null && t.row_count >= 0 ? ` · ~${t.row_count.toLocaleString()} rows` : ""}`),
      ...fact("Primary key", pk),
      ...fact(
        "Terms",
        mine.length ? mine.map((term) => `${term.name}${term.certified ? "" : " (not certified)"}${term.synonyms.length ? `: ${term.synonyms.join(", ")}` : ""}`).join("; ") : "",
      ),
      ...fact("Measures", list(measures.map((m) => `${m.name}${m.certified ? "" : " (draft)"}`))),
    ),
    joins.length
      ? el(
          "ul",
          { class: "plain" },
          ...joins.map((e) => {
            const other = e.from === name ? e.to : e.from;
            return el(
              "li",
              {},
              `${e.label} `,
              el("span", { class: "muted" }, e.declared ? "declared key" : e.verified === true ? "verified" : e.verified === false ? "contradicted" : "not checked"),
              " ",
              el("button", { type: "button", class: "link", onclick: () => select(other) }, `Go to ${other}`),
            );
          }),
        )
      : el("p", { class: "muted" }, "Joins to nothing the catalog knows of."),
  ];
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

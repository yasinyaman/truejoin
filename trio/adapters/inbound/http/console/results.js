"use strict";
// What the Query tab does with a result besides the table: a CSV of it, a chart
// when it has a label and a number, and the queries run in this browser.
// Uses el and $ from console.js. Colours come from classes in console.css (the
// CSP allows no inline style), so the chart follows the light and dark themes.

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}, ...children) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  e.append(...children.filter((c) => c !== null && c !== undefined));
  return e;
}

// -- CSV ------------------------------------------------------------------

function csvCell(value) {
  if (value === null || value === undefined) return "";
  const text = typeof value === "object" ? JSON.stringify(value) : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function toCsv(columns, rows) {
  const lines = [columns.map(csvCell).join(","), ...rows.map((row) => columns.map((c) => csvCell(row[c])).join(","))];
  // A byte-order mark, so a spreadsheet reads ş and ğ as UTF-8.
  return `﻿${lines.join("\r\n")}\r\n`;
}

function downloadCsv(columns, rows) {
  const url = URL.createObjectURL(new Blob([toCsv(columns, rows)], { type: "text/csv;charset=utf-8" }));
  const link = el("a", { href: url, download: `trio-result-${new Date().toISOString().slice(0, 19).replaceAll(":", "")}.csv` });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// -- chart ----------------------------------------------------------------

const MAX_POINTS = 60;
const DATE_LIKE = /^\d{4}(-\d{2}){0,2}([T ]\d{2}:\d{2}.*)?$/;

// Which column labels the rows and which hold numbers; null when there is nothing to chart.
function chartable(columns, rows) {
  if (rows.length < 2 || rows.length > MAX_POINTS) return null;
  const isNumber = (c) => rows.every((r) => r[c] === null || typeof r[c] === "number") && rows.some((r) => typeof r[c] === "number");
  const numbers = columns.filter(isNumber);
  const label = columns.find((c) => !numbers.includes(c));
  if (!label || !numbers.length) return null;
  const temporal = rows.length >= 3 && rows.every((r) => r[label] !== null && DATE_LIKE.test(String(r[label])));
  return { label, numbers, temporal };
}

function fmt(n) {
  return typeof n === "number" ? n.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "–";
}

// The axis: from zero (or below it), out to a round value, with a tick step of
// 1, 2 or 5 times a power of ten, about four ticks over the span.
function axis(values) {
  const lo0 = Math.min(0, ...values);
  const hi0 = Math.max(0, ...values);
  const raw = (hi0 - lo0 || Math.abs(hi0) || 1) / 4;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * power).find((s) => s >= raw);
  const lo = Math.floor(lo0 / step) * step;
  const hi = Math.ceil(hi0 / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 1e6; v += step) ticks.push(Number(v.toPrecision(12)));
  return { lo, hi, ticks };
}

// Ranked horizontal bars: one hue, at most 24px thick, a 4px rounded end at the value, zero as the baseline.
function barChart(rows, label, measure) {
  const values = rows.map((r) => r[measure]);
  const { lo, hi, ticks } = axis(values.filter((v) => v !== null));
  const labelWidth = Math.min(180, 8 + 7 * Math.max(...rows.map((r) => String(r[label] ?? "NULL").length)));
  const plot = 420;
  const band = 26;
  const bar = 18;
  const width = labelWidth + plot + 70;
  const height = band * rows.length + 28;
  const x = (v) => labelWidth + ((v - lo) / (hi - lo || 1)) * plot;
  const zero = x(0);
  const marks = rows.map((r, i) => {
    const v = r[measure];
    const y = 6 + i * band;
    const name = String(r[label] ?? "NULL");
    const parts = [svgEl("text", { x: labelWidth - 8, y: y + bar / 2 + 4, class: "chart-label", "text-anchor": "end" }, name.length > 24 ? `${name.slice(0, 23)}…` : name)];
    if (v !== null) {
      const end = x(v);
      const left = Math.min(zero, end);
      const w = Math.max(Math.abs(end - zero), 1);
      const r4 = Math.min(4, w);
      // Square at the baseline, rounded at the value's end.
      const d = v >= 0
        ? `M${left},${y}h${w - r4}a${r4},${r4} 0 0 1 ${r4},${r4}v${bar - 2 * r4}a${r4},${r4} 0 0 1 -${r4},${r4}h-${w - r4}z`
        : `M${left + w},${y}h-${w - r4}a${r4},${r4} 0 0 0 -${r4},${r4}v${bar - 2 * r4}a${r4},${r4} 0 0 0 ${r4},${r4}h${w - r4}z`;
      parts.push(
        svgEl("path", { d, class: "chart-mark", tabindex: "0" }, svgEl("title", {}, `${name}: ${fmt(v)}`)),
        svgEl("text", { x: v >= 0 ? end + 6 : end - 6, y: y + bar / 2 + 4, class: "chart-value", "text-anchor": v >= 0 ? "start" : "end" }, fmt(v)),
      );
    }
    return svgEl("g", {}, ...parts);
  });
  const grid = ticks.map((t) =>
    svgEl("g", {}, svgEl("line", { x1: x(t), x2: x(t), y1: 2, y2: height - 22, class: t === 0 ? "chart-baseline" : "chart-grid" }),
      svgEl("text", { x: x(t), y: height - 6, class: "chart-tick", "text-anchor": "middle" }, fmt(t))),
  );
  return svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, role: "img", "aria-label": `${measure} by ${label}` }, ...grid, ...marks);
}

// A line over time: 2px, a marker on each point carrying its value, the y axis from zero when all are positive.
function lineChart(rows, label, measure) {
  const points = rows.filter((r) => r[measure] !== null);
  const { lo, hi, ticks } = axis(points.map((r) => r[measure]));
  const left = 64;
  const plotW = 520;
  const plotH = 220;
  const width = left + plotW + 44; // room for the last date label, centred on its point
  const height = plotH + 44;
  const x = (i) => left + (points.length === 1 ? plotW / 2 : (i / (points.length - 1)) * plotW);
  const y = (v) => 10 + plotH - ((v - lo) / (hi - lo || 1)) * plotH;
  const grid = ticks.map((t) =>
    svgEl("g", {}, svgEl("line", { x1: left, x2: left + plotW, y1: y(t), y2: y(t), class: t === 0 ? "chart-baseline" : "chart-grid" }),
      svgEl("text", { x: left - 8, y: y(t) + 4, class: "chart-tick", "text-anchor": "end" }, fmt(t))),
  );
  const every = Math.ceil(points.length / 6);
  const xLabels = points.map((r, i) =>
    i % every === 0 || i === points.length - 1
      ? svgEl("text", { x: x(i), y: plotH + 30, class: "chart-tick", "text-anchor": "middle" }, String(r[label]).slice(0, 10))
      : null,
  );
  const path = points.map((r, i) => `${i ? "L" : "M"}${x(i)},${y(r[measure])}`).join("");
  const dots = points.map((r, i) =>
    svgEl("circle", { cx: x(i), cy: y(r[measure]), r: 4, class: "chart-dot", tabindex: "0" }, svgEl("title", {}, `${r[label]}: ${fmt(r[measure])}`)),
  );
  return svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, role: "img", "aria-label": `${measure} over ${label}` },
    ...grid, ...xLabels, svgEl("path", { d: path, class: "chart-line" }), ...dots);
}

// The table, or the chart: one measure at a time, never two scales on one axis.
function resultViews(columns, rows, table) {
  const spec = chartable(columns, rows);
  const holder = el("div", { class: "scroll" }, table);
  const tools = [el("button", { type: "button", class: "secondary", onclick: () => downloadCsv(columns, rows) }, "Download CSV")];
  if (spec) {
    let measure = spec.numbers[0];
    let showing = "table";
    const draw = () =>
      holder.replaceChildren(
        ...(showing === "table"
          ? [table]
          : [
              el("p", { class: "muted" }, `${measure} ${spec.temporal ? "over" : "by"} ${spec.label}`),
              (spec.temporal ? lineChart : barChart)(rows, spec.label, measure),
            ]),
      );
    const toggle = el("button", { type: "button", class: "secondary", "aria-pressed": "false" }, "Chart");
    toggle.addEventListener("click", () => {
      showing = showing === "table" ? "chart" : "table";
      toggle.textContent = showing === "table" ? "Chart" : "Table";
      toggle.setAttribute("aria-pressed", String(showing === "chart"));
      picker.hidden = showing === "table" || spec.numbers.length < 2;
      draw();
    });
    const picker = el("select", { "aria-label": "Measure to chart", hidden: true }, ...spec.numbers.map((n) => el("option", { value: n }, n)));
    picker.addEventListener("change", () => {
      measure = picker.value;
      draw();
    });
    tools.push(toggle, picker);
  }
  return [el("div", { class: "bar" }, ...tools), holder];
}

// -- history --------------------------------------------------------------

const HISTORY = "trio-console-history";
const HISTORY_SIZE = 30;

// This browser's own list, so it is lost with the browser's storage and never leaves it.
function readHistory() {
  try {
    const items = JSON.parse(localStorage.getItem(HISTORY) || "[]");
    return Array.isArray(items) ? items.filter((h) => h && typeof h.text === "string") : [];
  } catch {
    return [];
  }
}

function rememberQuery(mode, text) {
  const items = [{ mode, text, at: new Date().toISOString() }, ...readHistory().filter((h) => !(h.mode === mode && h.text === text))];
  try {
    localStorage.setItem(HISTORY, JSON.stringify(items.slice(0, HISTORY_SIZE)));
  } catch {}
  renderHistory();
}

function renderHistory() {
  const items = readHistory();
  const list = $("history");
  if (!list) return;
  list.replaceChildren(
    ...(items.length
      ? [
          ...items.map((h) =>
            el(
              "li",
              {},
              el(
                "button",
                {
                  type: "button",
                  class: "link",
                  title: h.text,
                  onclick: () => {
                    setMode(h.mode);
                    $("input").value = h.text;
                    $("input").focus();
                  },
                },
                el("span", { class: "badge" }, h.mode === "sql" ? "SQL" : "Q"),
                " ",
                h.text.length > 70 ? `${h.text.slice(0, 69)}…` : h.text,
              ),
            ),
          ),
          el("li", {}, el("button", { type: "button", class: "link muted", onclick: clearHistory }, "Clear the history")),
        ]
      : [el("li", { class: "muted" }, "Nothing run yet in this browser.")]),
  );
}

function clearHistory() {
  try {
    localStorage.removeItem(HISTORY);
  } catch {}
  renderHistory();
}

renderHistory();

"use strict";
// What the Query tab does with a result besides the table: a CSV of it, a chart
// when it has a label and a number (split into series by a second label, or
// several measures of one size together), and the queries run in this browser.
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
const MAX_SERIES = 6; // the validated palette's first six slots (console.css)
const DATE_LIKE = /^\d{4}(-\d{2}){0,2}([T ]\d{2}:\d{2}.*)?$/;

// Which column labels the rows, which (if any) splits them into series, and which hold
// numbers; null when there is nothing to chart. "month, city, amount" is amount per
// month, one line per city.
function chartable(columns, rows) {
  if (rows.length < 2) return null;
  const isNumber = (c) => rows.every((r) => r[c] === null || typeof r[c] === "number") && rows.some((r) => typeof r[c] === "number");
  const numbers = columns.filter(isNumber);
  const labels = columns.filter((c) => !numbers.includes(c));
  if (!labels.length || !numbers.length) return null;
  const label = labels[0];
  const split = labels.length > 1 ? labels[1] : null;
  const xs = new Set(rows.map((r) => String(r[label] ?? "NULL")));
  if (xs.size > MAX_POINTS || (!split && rows.length > MAX_POINTS)) return null;
  const temporal = xs.size >= 3 && rows.every((r) => r[label] !== null && DATE_LIKE.test(String(r[label])));
  // Several measures share an axis only when they are of one size; never two scales.
  const peaks = numbers.map((c) => Math.max(...rows.map((r) => Math.abs(r[c] ?? 0))));
  const together = !split && numbers.length > 1 && Math.max(...peaks) <= 10 * Math.max(Math.min(...peaks), 1e-9);
  return { label, split, numbers, temporal, together };
}

// The rows as series over one ordered set of labels: one series per value of the split
// column (the largest MAX_SERIES of them), or per measure, or the one measure alone.
function seriesOf(rows, spec, measures) {
  const xs = [...new Set(rows.map((r) => String(r[spec.label] ?? "NULL")))];
  if (spec.temporal) xs.sort();
  let series;
  if (spec.split) {
    const [measure] = measures;
    const groups = new Map();
    for (const r of rows) {
      const name = String(r[spec.split] ?? "NULL");
      if (!groups.has(name)) groups.set(name, new Map());
      if (r[measure] !== null) groups.get(name).set(String(r[spec.label] ?? "NULL"), r[measure]);
    }
    series = [...groups].map(([name, values]) => ({ name, values }));
  } else {
    series = measures.map((m) => ({ name: m, values: new Map(rows.filter((r) => r[m] !== null).map((r) => [String(r[spec.label] ?? "NULL"), r[m]])) }));
  }
  const size = (s) => [...s.values.values()].reduce((a, v) => a + Math.abs(v), 0);
  const kept = series.length > MAX_SERIES ? [...series].sort((a, b) => size(b) - size(a)).slice(0, MAX_SERIES) : series;
  // Colour follows the series, in the order it first appears, never its rank.
  const shown = series.filter((s) => kept.includes(s));
  return { xs, series: shown, hidden: series.length - shown.length };
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

// A bar from the baseline to its value: square at zero, a 4px rounded end at the value.
function barPath(zero, end, y, thick) {
  const left = Math.min(zero, end);
  const w = Math.max(Math.abs(end - zero), 1);
  const r4 = Math.min(4, w, thick / 2);
  return end >= zero
    ? `M${left},${y}h${w - r4}a${r4},${r4} 0 0 1 ${r4},${r4}v${thick - 2 * r4}a${r4},${r4} 0 0 1 -${r4},${r4}h-${w - r4}z`
    : `M${left + w},${y}h-${w - r4}a${r4},${r4} 0 0 0 -${r4},${r4}v${thick - 2 * r4}a${r4},${r4} 0 0 0 ${r4},${r4}h${w - r4}z`;
}

// Horizontal bars, one band per label and one bar per series in it, at most 24px thick,
// zero as the baseline. One series gets its value at the end of each bar.
function barChart({ xs, series }, title) {
  const values = series.flatMap((s) => [...s.values.values()]);
  const { lo, hi, ticks } = axis(values);
  const labelWidth = Math.min(180, 8 + 7 * Math.max(...xs.map((x) => x.length)));
  const plot = 420;
  const k = series.length;
  const thick = k === 1 ? 18 : Math.max(6, Math.min(14, 30 / k));
  const gap = 2; // the surface between neighbouring bars
  const band = k * thick + (k - 1) * gap + 8;
  const width = labelWidth + plot + 70;
  const height = band * xs.length + 28;
  const x = (v) => labelWidth + ((v - lo) / (hi - lo || 1)) * plot;
  const zero = x(0);
  const marks = xs.map((name, i) => {
    const top = 6 + i * band;
    const parts = [svgEl("text", { x: labelWidth - 8, y: top + (band - 8) / 2 + 4, class: "chart-label", "text-anchor": "end" }, name.length > 24 ? `${name.slice(0, 23)}…` : name)];
    series.forEach((s, j) => {
      const v = s.values.get(name);
      if (v === undefined) return;
      const y = top + j * (thick + gap);
      const who = k === 1 ? name : `${name} · ${s.name}`;
      parts.push(svgEl("path", { d: barPath(zero, x(v), y, thick), class: `chart-mark series-${j}`, tabindex: "0" }, svgEl("title", {}, `${who}: ${fmt(v)}`)));
      if (k === 1) {
        parts.push(svgEl("text", { x: v >= 0 ? x(v) + 6 : x(v) - 6, y: y + thick / 2 + 4, class: "chart-value", "text-anchor": v >= 0 ? "start" : "end" }, fmt(v)));
      }
    });
    return svgEl("g", {}, ...parts);
  });
  const grid = ticks.map((t) =>
    svgEl("g", {}, svgEl("line", { x1: x(t), x2: x(t), y1: 2, y2: height - 22, class: t === 0 ? "chart-baseline" : "chart-grid" }),
      svgEl("text", { x: x(t), y: height - 6, class: "chart-tick", "text-anchor": "middle" }, fmt(t))),
  );
  return svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, role: "img", "aria-label": title }, ...grid, ...marks);
}

// Lines over time, 2px, a marker on each point carrying its value; up to four series
// are also named at their last point. The y axis from zero when all are positive.
function lineChart({ xs, series }, title) {
  const values = series.flatMap((s) => [...s.values.values()]);
  const { lo, hi, ticks } = axis(values);
  const left = 64;
  const plotW = 520;
  const plotH = 220;
  const named = series.length > 1 && series.length <= 4;
  const right = named ? 44 + 7 * Math.min(18, Math.max(...series.map((s) => s.name.length))) : 44;
  const width = left + plotW + right;
  const height = plotH + 44;
  const x = (i) => left + (xs.length === 1 ? plotW / 2 : (i / (xs.length - 1)) * plotW);
  const y = (v) => 10 + plotH - ((v - lo) / (hi - lo || 1)) * plotH;
  const grid = ticks.map((t) =>
    svgEl("g", {}, svgEl("line", { x1: left, x2: left + plotW, y1: y(t), y2: y(t), class: t === 0 ? "chart-baseline" : "chart-grid" }),
      svgEl("text", { x: left - 8, y: y(t) + 4, class: "chart-tick", "text-anchor": "end" }, fmt(t))),
  );
  const every = Math.ceil(xs.length / 6);
  const xLabels = xs.map((name, i) =>
    i % every === 0 || i === xs.length - 1
      ? svgEl("text", { x: x(i), y: plotH + 30, class: "chart-tick", "text-anchor": "middle" }, name.slice(0, 10))
      : null,
  );
  const lines = series.flatMap((s, j) => {
    const points = xs.map((name, i) => [i, s.values.get(name)]).filter(([, v]) => v !== undefined);
    if (!points.length) return [];
    const d = points.map(([i, v], n) => `${n ? "L" : "M"}${x(i)},${y(v)}`).join("");
    const who = (name) => (series.length === 1 ? name : `${name} · ${s.name}`);
    const [li, lv] = points[points.length - 1];
    return [
      svgEl("path", { d, class: `chart-line series-${j}` }),
      ...points.map(([i, v]) => svgEl("circle", { cx: x(i), cy: y(v), r: 4, class: `chart-dot series-${j}`, tabindex: "0" }, svgEl("title", {}, `${who(xs[i])}: ${fmt(v)}`))),
      named ? svgEl("text", { x: x(li) + 8, y: y(lv) + 4, class: "chart-value" }, s.name.length > 18 ? `${s.name.slice(0, 17)}…` : s.name) : null,
    ];
  });
  return svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, role: "img", "aria-label": title }, ...grid, ...xLabels, ...lines);
}

// The key to the colours, whenever there is more than one series; text stays in ink.
function legend(series, hidden) {
  return el(
    "ul",
    { class: "chart-legend", "aria-label": "Series" },
    ...series.map((s, j) => el("li", {}, el("span", { class: `swatch series-${j}`, "aria-hidden": "true" }), s.name)),
    hidden ? el("li", { class: "muted" }, `${hidden} more in the table`) : null,
  );
}

// The table, or the chart: one measure at a time unless several share a scale.
function resultViews(columns, rows, table) {
  const spec = chartable(columns, rows);
  const holder = el("div", { class: "scroll" }, table);
  const tools = [el("button", { type: "button", class: "secondary", onclick: () => downloadCsv(columns, rows) }, "Download CSV")];
  if (spec) {
    const ALL = ""; // no column is called that
    let measure = spec.numbers[0];
    let showing = "table";
    const draw = () => {
      if (showing === "table") return holder.replaceChildren(table);
      const measures = measure === ALL ? spec.numbers : [measure];
      const data = seriesOf(rows, spec, measures);
      const what = measure === ALL ? spec.numbers.join(", ") : measure;
      const title = `${what} ${spec.temporal ? "over" : "by"} ${spec.label}${spec.split ? `, per ${spec.split}` : ""}`;
      holder.replaceChildren(
        el("p", { class: "muted" }, title),
        data.series.length > 1 ? legend(data.series, data.hidden) : null,
        (spec.temporal ? lineChart : barChart)(data, title),
      );
    };
    const toggle = el("button", { type: "button", class: "secondary", "aria-pressed": "false" }, "Chart");
    const choices = [...spec.numbers.map((n) => [n, n]), ...(spec.together ? [[ALL, "all of them"]] : [])];
    const picker = el("select", { "aria-label": "Measure to chart", hidden: true }, ...choices.map(([v, t]) => el("option", { value: v }, t)));
    toggle.addEventListener("click", () => {
      showing = showing === "table" ? "chart" : "table";
      toggle.textContent = showing === "table" ? "Chart" : "Table";
      toggle.setAttribute("aria-pressed", String(showing === "chart"));
      picker.hidden = showing === "table" || choices.length < 2;
      draw();
    });
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

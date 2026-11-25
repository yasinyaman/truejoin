"use strict";
// The console's language: English as written, or Turkish from tr.js. The choice is
// the visitor's (kept in this browser), else the browser's own language.
//
// tr("Save") and tr`Remove ${name}?` (key "Remove {0}?") give the text in that language;
// a missing entry gives the English. Only text written in these scripts goes through
// tr: a value from the server (a column called "Name") is shown as it is.
const LANG_STORE = "truejoin-console-lang";
const LANG = (() => {
  try {
    const kept = localStorage.getItem(LANG_STORE);
    if (kept === "tr" || kept === "en") return kept;
  } catch {}
  return (navigator.language || "en").toLowerCase().startsWith("tr") ? "tr" : "en";
})();
const WORDS = LANG === "tr" ? TR : {};

function tr(strings, ...values) {
  if (typeof strings === "string") return WORDS[strings] ?? strings;
  if (!Array.isArray(strings)) return strings; // not text: a number, null
  const known = WORDS[strings.reduce((key, part, i) => `${key}{${i - 1}}${part}`)];
  if (known === undefined) return strings.reduce((text, part, i) => `${text}${values[i - 1]}${part}`);
  return known.replace(/\{(\d+)\}/g, (_, n) => String(values[Number(n)]));
}

// The page's own text: elements marked data-t, and their marked attributes
// (data-t-label, data-t-placeholder, and data-t-attrs for any other).
(() => {
  document.documentElement.lang = LANG;
  for (const node of document.querySelectorAll("[data-t]")) node.textContent = tr(node.textContent.trim());
  for (const node of document.querySelectorAll("[data-t-label]")) node.setAttribute("aria-label", tr(node.getAttribute("aria-label")));
  for (const node of document.querySelectorAll("[data-t-placeholder]")) node.setAttribute("placeholder", tr(node.getAttribute("placeholder")));
  // A component's own text attributes (Carbon's menu-bar-label), named one by one.
  for (const node of document.querySelectorAll("[data-t-attrs]")) {
    for (const name of node.dataset.tAttrs.split(" ")) node.setAttribute(name, tr(node.getAttribute(name)));
  }
  const pick = document.getElementById("lang");
  pick.value = LANG;
  pick.addEventListener("change", () => {
    try {
      localStorage.setItem(LANG_STORE, pick.value);
    } catch {}
    location.reload();
  });
})();

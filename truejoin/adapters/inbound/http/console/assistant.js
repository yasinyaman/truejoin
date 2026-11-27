"use strict";
// The built-in assistant on the Query screen: a model answers a question by calling the
// tools as the person asking. The steps are shown as they happen, then the answer, with
// the rows it read last and the SQL that ran. Uses el, $, api, note, showRows, withAuth,
// freshToken and ACCESS from console.js, which is loaded after this file and chooses a
// mode as it starts: nothing here runs before it is called.
//
// The server keeps no conversation: what was asked and answered before is held here, in
// the page, and sent along with each question. It is forgotten when somebody else signs in.

let talk = []; // earlier questions and answers, as they are sent along
let talkId = ""; // this conversation's name in the audit trail
let asking = null; // the AbortController of the run in flight

function assistantAvailable() {
  return ACCESS.assistant === true;
}

function newTalkId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("");
}

// A new conversation, or another caller: nothing of the last one is kept or shown.
function forgetConversation() {
  asking?.abort();
  talk = [];
  talkId = "";
  $("assistant").replaceChildren();
  $("assistant").hidden = true;
  $("assistant-new").hidden = true;
}

// What the mode shows of itself when it is not the one chosen: nothing.
function showAssistant(chosen) {
  $("assistant").hidden = !chosen || !$("assistant").childElementCount;
  $("assistant-new").hidden = !chosen || !talk.length;
  $("assistant-stop").hidden = !chosen || !asking;
}

// Which model answers, and what it is shown: said before the first question, not after.
async function assistantHelp() {
  const box = $("question-help");
  try {
    const status = await api("GET", "/olap/assistant");
    if (mode !== "assistant" || !status.available) return;
    const model = `${status.provider}/${status.model}`;
    const parts = [
      el("p", {}, tr`A model (${model}) answers by calling the tools as you: it reads what you may read, and every call is in the audit trail under your name.`),
      status.masks_everyone
        ? el("p", {}, tr("That model is outside this server, so values are masked for it as for someone with no roles, and in your answer too."))
        : null,
      el("p", { class: "muted" }, tr("Its answer is written by a model: check the figures against the rows and the SQL shown with it.")),
    ];
    box.replaceChildren(...parts.filter(Boolean));
    box.hidden = false;
  } catch {} // the run itself will say what is wrong
}

// A POST whose answer is read a line at a time, each line one JSON object.
async function streamLines(path, body, onLine, signal) {
  await freshToken();
  const response = await fetch(path, { method: "POST", headers: withAuth({ "Content-Type": "application/json" }), body: JSON.stringify(body), signal });
  if (!response.ok) {
    let data = {};
    try {
      data = await response.json();
    } catch {}
    const detail = data.detail ?? data.error;
    const error = new Error(typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : response.statusText || tr("The request failed"));
    error.status = response.status;
    throw error;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let rest = "";
  for (;;) {
    const { value, done } = await reader.read();
    rest += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = rest.split("\n");
    rest = lines.pop();
    for (const line of lines) if (line.trim()) onLine(JSON.parse(line));
    if (done) break;
  }
  if (rest.trim()) onLine(JSON.parse(rest));
}

// One tool the model asked for: its name and what it was asked, then how it went.
function stepNode(step) {
  const sql = step.tool === "query_data" && typeof step.arguments.sql === "string" ? step.arguments.sql : "";
  const asked = sql || JSON.stringify(step.arguments);
  const went = el("span", { class: "muted" }, tr("Running…"));
  const node = el(
    "li",
    {},
    el("code", {}, step.tool),
    " ",
    went,
    asked && asked !== "{}" ? el("details", {}, el("summary", {}, sql ? "SQL" : tr("Arguments")), el("pre", {}, asked)) : null,
  );
  node.finish = (result) => {
    if (result.error) {
      went.className = "error";
      went.textContent = result.error;
      return;
    }
    const parts = [result.row_count === null ? "" : result.row_count === 0 ? tr("No rows") : tr`${result.row_count} row(s)`, tr`${Math.round(result.duration_ms)} ms`];
    went.textContent = parts.filter(Boolean).join(" · ");
  };
  return node;
}

// The answer's words, as text: a model wrote them, so nothing in them is a link or markup.
function answerNode(answer) {
  return el(
    "div",
    { class: "answered" },
    ...answer.text.split(/\n{2,}/).map((part) => el("p", {}, part)),
    answer.outcome === "exhausted" ? el("p", { class: "warn" }, tr("The model ran out of steps: this answer is from what it had read by then.")) : null,
  );
}

async function askAssistant(text) {
  const steps = el("ol", { class: "steps plain" });
  const said = el("div", {});
  $("assistant").append(el("article", { class: "exchange" }, el("p", { class: "asked" }, text), steps, said));
  $("assistant").hidden = false;
  talkId ||= newTalkId();
  asking = new AbortController();
  $("assistant-stop").hidden = false;
  const open = new Map(); // a step's node, until its result comes
  let answer = null;
  try {
    await streamLines(
      "/olap/assistant?stream=true",
      { question: text, history: talk, conversation: talkId },
      (event) => {
        if (event.type === "step" && event.phase === "call") {
          open.set(event.index, stepNode(event));
          steps.append(open.get(event.index));
        } else if (event.type === "step") open.get(event.index)?.finish(event);
        else if (event.type === "answer") answer = event;
        else if (event.type === "error") said.replaceChildren(el("p", { class: "error" }, event.message));
      },
      asking.signal,
    );
  } catch (e) {
    said.replaceChildren(e.name === "AbortError" ? el("p", { class: "muted" }, tr("Stopped.")) : el("p", { class: "error" }, e.message));
  } finally {
    asking = null;
    $("assistant-stop").hidden = true;
  }
  $("status").textContent = "";
  if (!answer) return;
  said.replaceChildren(answerNode(answer));
  // Only its newest part is used by the server; the page need not keep more.
  talk = [...talk, { question: text, answer: answer.text }].slice(-20);
  $("assistant-new").hidden = false;
  $("input").value = "";
  $("status").textContent = tr`${answer.steps} step(s), ${answer.tool_calls} tool call(s)`;
  const notes = [];
  if (answer.result?.masked?.length) notes.push(el("p", { class: "muted" }, tr`Masked for you: ${answer.result.masked.join(", ")}. Your key sees these columns as the masking rules show them.`));
  for (const sql of answer.sql) notes.push(note(tr("SQL that ran"), sql));
  $("notes").replaceChildren(...notes);
  if (answer.result) showRows(answer.result);
}

// Wired once the page is there; console.js, which defines $, is loaded after this file.
document.addEventListener("DOMContentLoaded", () => {
  $("assistant-stop").addEventListener("click", () => asking?.abort());
  $("assistant-new").addEventListener("click", () => {
    forgetConversation();
    $("notes").replaceChildren();
    $("result").replaceChildren();
    $("status").textContent = "";
    $("input").focus();
  });
});

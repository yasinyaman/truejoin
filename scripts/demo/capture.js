// Keeps what a Claude Code session said and did, from its stream on stdin:
//
//   claude -p "<the question>" --mcp-config mcp.json --strict-mcp-config --tools "" \
//     --allowedTools mcp__truejoin --output-format stream-json --verbose < /dev/null \
//     | node scripts/demo/capture.js > scripts/demo/claude-session.json
//
// What it keeps: the question, each tool call with its arguments, each tool
// result as the server answered it, and the answer, in the order they came.
// What it drops: everything about the machine and the account the session ran
// on (paths, session ids, the tool list, usage and cost). Nothing is reworded.
"use strict";
const lines = require("fs").readFileSync(0, "utf8").split("\n").filter(Boolean);
const events = [];
const calls = new Map();
let prompt = process.env.PROMPT || "";
let outcome = {};

const text = (content) => (typeof content === "string" ? content : (content || []).map((part) => part.text || "").join(""));
// An MCP result arrives as {"result": "<the tool's JSON>"}: the tool's own text is what is kept.
const unwrap = (body) => {
  try {
    const parsed = JSON.parse(body);
    return typeof parsed.result === "string" ? parsed.result : body;
  } catch {
    return body;
  }
};

for (const line of lines) {
  const event = JSON.parse(line);
  if (event.type === "assistant") {
    for (const part of event.message.content) {
      if (part.type === "text" && part.text.trim()) events.push({ type: "text", text: part.text });
      if (part.type === "tool_use") {
        const tool = part.name.replace(/^mcp__[^_]+__/, "");
        calls.set(part.id, tool);
        events.push({ type: "tool_use", tool, input: part.input });
      }
    }
  } else if (event.type === "user") {
    for (const part of [].concat(event.message.content)) {
      if (part.type === "tool_result") events.push({ type: "tool_result", tool: calls.get(part.tool_use_id) || "", error: Boolean(part.is_error), text: unwrap(text(part.content)) });
      else if (part.type === "text" && !prompt) prompt = part.text;
    }
  } else if (event.type === "result") {
    outcome = { ended: event.subtype, turns: event.num_turns, seconds: Math.round(event.duration_ms / 100) / 10 };
  }
}
if (!prompt) throw new Error("the question is not in the stream: pass it as PROMPT");
process.stdout.write(`${JSON.stringify({ client: "Claude Code", server: "truejoin (MCP, streamable HTTP)", prompt, ...outcome, events }, null, 1)}\n`);

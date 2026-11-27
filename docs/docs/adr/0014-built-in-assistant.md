# 14. The built-in assistant is one more caller, not a wider one

- Status: Accepted
- Date: 2026-11-27

## Context

Until now a model touched a query in one place: with
`olap.unread_questions: sql` it wrote one statement for a question the
certified vocabulary read none of. No tool loop, no retry, no conversation.
People asked for the rest: a question in their own words, answered in several
steps, from inside the product.

Every way of building that risks the thing the product is for. An agent with
its own database credentials reads around the row rules. One that keeps a
conversation on the server keeps data nobody decided to keep. One whose
answer is rendered as markup can be made, by a row, to draw an image whose
address carries another row away. And a model outside the building is being
sent rows.

## Decision

The assistant is a loop over the tools that already exist, run as the caller.

- **Each tool call goes through `ToolService.for_assistant(scope)`**: the
  caller's `AccessScope`, so the same rewrite applies row rules and masks,
  and the same audit events are written, marked `via: assistant`. There is no
  second path to the data to keep in step with the first.
- **Only tools that read** (`ASSISTANT_TOOLS`). A tool outside the list is
  answered to the model as unknown and never run.
- **The model is told table names and nothing else.** Columns, counts and
  values come through the tools, which already say to each caller what that
  caller may know.
- **A run ends**: a step budget, then one turn that must be words; a deadline
  that also bounds each wait for the model; a bounded number of runs at once.
- **The server keeps no conversation.** The caller sends what was said
  before; it is theirs, bounded, and can change only their own answer.
- **Rows leave only where that was said.** With a cloud provider the
  configuration refuses to load without
  `analysis.share_samples_with_cloud_llm`, and every caller's run is under
  the default masks (`AccessScope.under_default_masks`), exempt roles
  included: allowed to read is not allowed to send.
- **The answer is text.** The console renders nothing a model wrote as a link
  or an image.
- **One record per run** (`assistant_run`): who, which model, how many turns
  and calls, how it ended. Never the answer, the arguments or a value.

It is off by default, and ships in 1.0.0 that way.

## Consequences

- The Question mode stays what it was: no model at query time, the same
  reading every time. The assistant is the other trade, said as such on the
  screen: more questions answered, each to be checked against the rows and
  SQL shown with it.
- A new tool is the assistant's only when it is added to `ASSISTANT_TOOLS`.
  A tool that writes or loads does not belong there.
- The model's port (`ChatModel`) is separate from `TextGenerator`: analysis
  needs a prompt and a text, the assistant a conversation and tools. Four
  providers implement it; each shape is tested through the real SDK.
- An on-premises OpenAI-compatible server counts as a cloud provider, because
  a provider is judged by its name. That errs on the side of masking.
- Under the default masks an exempt administrator's own answer is masked when
  the model is outside. The alternative, showing them raw values a model had
  not seen, would make the table disagree with the answer written from it.
- What a real model does with twelve tools is not something a scripted test
  shows. The step budget bounds the cost of a model that calls tools badly;
  it does not make its answer right.

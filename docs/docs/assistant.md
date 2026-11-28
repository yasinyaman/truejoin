# The built-in assistant

A model that answers a question about your data by calling Truejoin's own
tools, as the person asking. It is in the console's Query screen (the
**Assistant** mode) and at `POST /olap/assistant`. It is off unless you turn it
on.

It is not a way around anything. Every tool the model calls runs under the
caller's row rules and masks and leaves the same audit events as if the caller
had called it. The model is one more reader behind the same gate, not a
service account with a wider view.

| | Question mode | Assistant |
|---|---|---|
| Who reads the question | The certified business terms, no model | A model |
| Same question, same answer | Always | Not promised |
| Needs | An approved catalog with certified terms | A model that can call tools |
| What it can ask of the data | One measure, breakdowns, conditions | Anything the read-only tools answer, in several steps |
| What to check | The reading it shows | The figures, against the rows and the SQL it shows |

Use the Question mode where the vocabulary covers the question: it cannot
invent. Use the assistant for what the vocabulary does not cover, and read its
answer as a draft.

## Turning it on

```yaml
settings:
  llm:
    reasoning:
      provider: ollama            # or openai, anthropic, gemini
      model: qwen2.5:14b          # a model that supports tool calling
      base_url: http://ollama.internal:11434
olap:
  assistant:
    enabled: true
```

Then `truejoin config validate -c <config>`. The settings are in
[`config/reference.yaml`](https://github.com/yasinyaman/truejoin/blob/main/config/reference.yaml)
under `olap.assistant`, and can be changed from the console's Settings page
(`enabled` and `slot` with the name typed back, since both decide where rows
are sent).

| Setting | Default | What it bounds |
|---|---|---|
| `enabled` | `false` | Whether there is an assistant at all |
| `slot` | `reasoning` | Which of the `llm` models it uses |
| `max_steps` | 8 | Turns the model may take before it must answer from what it has read |
| `timeout` | 120 | Seconds for one question |
| `rate_limit` | `10/minute` | Questions per API key |
| `history_turns` | 6 | Earlier questions and answers sent along |
| `max_concurrent` | 4 | Questions answered at once |

With no usable model (no key, an unknown provider) the server still starts;
the route answers 409 with the reason and the console does not offer the mode.

## Where the rows go

What the tools return is sent to the model. That is the point of the feature,
and the one thing to decide before turning it on.

- **A model on your own servers** (`provider: ollama`) is shown what the
  caller may see: the caller's rows, masked as for the caller.
- **A cloud provider** (`openai`, `anthropic`, `gemini`) is outside the
  server. Two things then hold:
    - The configuration **refuses to load** unless
      `settings.analysis.share_samples_with_cloud_llm` is `true`. Turning the
      assistant on never sends rows anywhere by accident.
    - Every caller's run is under the **default masks**, the ones a caller
      with no roles gets, exempt roles included. Being allowed to read a value
      is not being allowed to send it away. The answer's own table is what the
      model read, so it is masked too. `GET /olap/assistant` says so
      (`masks_everyone`), and so does the console before the first question.

A provider is judged by its name. An OpenAI-compatible server of your own
(`provider: openai` with a `base_url` inside your network) counts as a cloud
provider: it needs the opt-in and gets the default masks.

Where masking is not configured (`settings.masking.enabled: false`, or no
rule for a caller with no roles) there are no masks to force: a cloud model
is then shown what the caller sees.

## What the model can do

It is told the tables by name and twelve tools, the ones that read:
`list_sources`, `describe_table`, `query_data`, `search_data`,
`aggregate_data`, `list_metrics`, `query_metrics`, `find_examples`,
`search_terms`, `get_term`, `check_question`, `answer_question` (the last
four only while `olap.terms` is on). It cannot load a table, create or refresh
a view, or read the server's own state. A tool outside the list is named back
to it as unknown and never run.

A tool's result is data to the model, not instructions: the system prompt
says so, and whatever a row says, the model's tools stay read-only and stay
the caller's. The console shows the answer as plain text: a link or an image a
model wrote does not become one.

A model's reasoning stays the model's. Where a provider returns reasoning
blocks with a tool call (Anthropic's thinking, Gemini's thought signatures),
they are sent back to that provider unchanged with the tool's result, as it
requires, and go nowhere else: not to the caller, not to the trail.

When a provider declines a request, the run fails and says so. Truejoin does
not ask a provider to retry on another of its models: that would send the
conversation, rows included, to a model nobody configured.

A run ends in one of four ways, each recorded:

| Outcome | What happened |
|---|---|
| `answered` | The model answered in words |
| `exhausted` | It ran out of steps and answered from what it had read |
| `failed` | The model failed, ran out of time, gave no answer, or the caller stopped listening |
| `refused` | Every slot was busy |

## Asking over REST

```bash
curl -s https://truejoin.example.com/olap/assistant \
  -H 'X-API-Key: <key>' -H 'Content-Type: application/json' \
  -d '{"question": "How many customers do I have, by segment?"}'
```

The answer is one document: `text`, `sql` (the statements that ran), `result`
(the rows read last, at most 100), `steps` (each tool, how it went, rows and
milliseconds), `outcome`, `turns` and `usage` (tokens, when the provider
reports them).

With `?stream=true` the answer comes one JSON object per line as it happens:
`step` objects (`phase` `call`, then `result`), then one `answer` or `error`.

For a follow-up, send what was asked and answered before; the server keeps no
conversation:

```json
{
  "question": "and only the active ones?",
  "history": [{"question": "How many customers do I have, by segment?", "answer": "..."}],
  "conversation": "any-name-of-yours"
}
```

`history` is the caller's own record, so it is bounded (the newest
`history_turns`, a few thousand characters) and can change nothing but the
caller's own answer. `conversation` is kept in the audit trail so that the
questions of one conversation can be read together.

| Status | When |
|---|---|
| 409 | The assistant is off, or has no usable model (the body says which) |
| 422 | The body is not a question |
| 429 | Its rate limit is spent, or every slot is busy |
| 502 | The model failed |
| 504 | The run was out of time |

The route needs the `query` permission, as every analytical route does.

## In the audit trail

One question leaves, under one request id:

- an `assistant_run` event: who asked, which provider and model, how many
  turns and tool calls, how long, tokens, how it ended;
- a `tool_call` event for each tool, as the caller, marked `via: assistant`;
- the `data_access` events of the reads behind them, with what was withheld.

Never the answer, the arguments, the SQL or a value. The words of the question
only where `settings.audit.question_text` keeps questions. In the console's
Audit page **Its calls** on a run lists the rest; over REST it is
`GET /api/v1/audit?request_id=<id>`.

## What has and has not been tried

The loop, the scope, the trail, the route and the console are tested with a
scripted model. Each provider's request and response shapes are tested through
its real SDK over a transport that answers from the test.

**Run against a live model: Anthropic, `claude-opus-5-5`, on 2026-11-28**, with
`scripts/demo/assistant_check.py` on the sample setup (commit `fafcb63`).
Every check passed, and all twelve requests to the API were answered:

- It called the tools unprompted (`answer_question` first, then
  `describe_table` and `query_data`), with its reasoning blocks returned to
  the API between turns.
- The tenant's key was told its own 2 customers, not all 3, and totals by
  city equal to the same key's SQL.
- Asked for national ids, it gave the masked values and said they were
  masked, for the tenant and, the model being outside the server, for the
  administrator too.
- Asked about the weather, it called no tool and said the data holds none.
- Five questions took 12 turns and about 52,500 input and 1,500 output
  tokens: most of each turn is the twelve tools' descriptions.

It also wrote one answer with Markdown emphasis, which the console shows as
typed; the instructions now ask for none (that wording has not been run live).

The console's Assistant mode ran with the same model the same day, for the
README's film (`scripts/demo/record.js`, its `assistant` scene): five tool
calls and an answer in about sixteen seconds, with no Markdown in it.

**Not run live:** the OpenAI-compatible, Gemini and Ollama adapters, which
have the SDK-level tests only. Small local models call tools
less reliably than the one above; the step budget and the `exhausted` outcome
are what bound that.

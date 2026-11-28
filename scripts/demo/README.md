# The sample setup, its report and the demo videos

A Truejoin to look at, on made-up data: two databases on two engines, every
console page on, and a report that joins the two. The film in the README is
two recordings made on it, joined, not drawn: a browser walks the console,
and an agent's session is a real one. This directory holds the setup and what records the
videos, so they can be made again when a screen changes.

| File | What it is |
|---|---|
| `docker-compose.yml` | The sample setup in one command: the two databases and the image built from this checkout |
| `config.yaml` | The setup itself: both databases, every console page on, three sample keys |
| `crm.sql` | The second database's tables and rows (a MySQL); the first is `docker/smoke/init.sql` (a PostgreSQL) |
| `catalogs/`, `make_catalog.py` | The approved catalogs of both databases, and what writes them |
| `report.py` | Writes a report joined from the two databases, as one key may read it |
| `check.py` | Whether a started setup answers as it should (`make demo-check`, and CI) |
| `assistant_check.py` | Whether the built-in assistant behaves with a real model behind it (not in CI: it needs one) |
| `record.js` | Films the console (the first half), the assistant's scene with a real model |
| `mcp.json` | How an MCP client reaches the sample setup, as the key `claude-agent` |
| `capture.js` | Keeps what a Claude Code session said and did, from its stream |
| `claude-session.json` | The session the second video shows, whole |
| `replay.html`, `record-claude.js` | Play that session as a terminal and film it, then the audit page |
| `overlay.js` | The pointer and the caption drawn over a page while it is filmed |
| `encode.sh` | Turns a recording into an MP4 and a GIF |
| `join.sh` | Joins the two into the film under `docs/docs/assets/` (`truejoin-demo.mp4`, `.gif`) |

## Running the sample setup

The built-in assistant is off in this setup, which needs no model. A comment
at the top of `config.yaml` says what to add to try it.

With Docker, one command from the repository's root builds the image from this
checkout and starts it beside the two databases: a PostgreSQL with a bank's
branches, customers and transactions (`smoke`), and a MySQL with a CRM's branch
targets and customer segments (`crm`). Five small tables of made-up data.

```bash
docker compose -f scripts/demo/docker-compose.yml up --build -d --wait
```

The console is then at `http://localhost:8000/ui` (`make demo-up` is the same
command; `DEMO_PORT=8080` in front of it serves it on another port). Sign in
with a key from `config.yaml`: `demo-admin-6c2f8b15e7d3` opens everything,
`demo-tenant-a-4f1c9d27b6e0` reads tenant a's rows, in both databases, with
the national ids and phone numbers masked. The first run builds the image and
pulls the two database images, which takes a few minutes and needs the
network. To stop it, and with `-v` to forget what it wrote:

```bash
docker compose -f scripts/demo/docker-compose.yml down
```

The port is bound to this machine alone: the keys are samples written in a
public repository, and this is not a configuration to deploy. The key that
encrypts what the console stores is made on the first start and kept in a
volume, so none is in the repository. CI starts the setup this way on every
pull request and checks that it answers (`make demo-check`,
`scripts/demo/check.py`).

## A report joined from two databases

The two databases know nothing of each other. Truejoin loads what a query
needs from each and joins them, after each caller's row rules and masks have
been applied to both. In the console, on the Query screen:

```sql
select s.sehir, h.hedef_tutar as hedef, sum(i.tutar) as gerceklesen
from smoke.islemler i
join smoke.musteriler m on m.musteri_id = i.musteri_id
join smoke.subeler s on s.sube_kodu = m.sube_kodu
join crm.sube_hedefleri h on h.sube_kodu = s.sube_kodu
group by 1, 2 order by 1
```

`smoke.*` is the PostgreSQL, `crm.*` the MySQL. The same statement, with the
ratio of the two, is saved in the `crm` catalog as a certified analysis
(Catalog, `crm`, Overview, Saved analyses), where **Run it** runs it and an
agent finds it with `find_examples`. `report.py` asks the server for the saved
analyses and writes them out as Markdown, as the key it is given:

```bash
python3 scripts/demo/report.py --as admin
```

```bash
python3 scripts/demo/report.py --as tenant_a --out demo-out/report-a.md
```

The administrator's report counts every tenant's transactions (Istanbul: 1,149
against a target of 1,200); tenant a's counts its own (150 against the same
target). Neither run is special to the script: it holds no SQL and has no
access of its own, and each query is an event in the audit trail under the
key's name. `python3 scripts/demo/report.py --help` has the options for
another server.

One thing to know when saving an analysis: one that quotes a value, a number
in `ROUND(x, 1)` included, is not offered to a caller held to row rules or
masks. The two here are written without a constant for that reason.

### From source

The films are recorded on the same setup run from source, which is also how to
work on the console while looking at it. It needs the two databases, each with
its file loaded:

```bash
docker run -d --name truejoin-demo-db -p 5432:5432 \
  -e POSTGRES_USER=smoke -e POSTGRES_PASSWORD=smoke -e POSTGRES_DB=smoke \
  -v "$PWD/docker/smoke/init.sql:/docker-entrypoint-initdb.d/01-init.sql:ro" postgres:16-alpine
```

```bash
docker run -d --name truejoin-demo-crm -p 3306:3306 \
  -e MYSQL_DATABASE=crm -e MYSQL_USER=crm -e MYSQL_PASSWORD=crm -e MYSQL_RANDOM_ROOT_PASSWORD=yes \
  -v "$PWD/scripts/demo/crm.sql:/docker-entrypoint-initdb.d/01-crm.sql:ro" mysql:8.0
```

Then, from the repository's root:

```bash
mkdir -p demo-out && cp -r scripts/demo/catalogs demo-out/catalogs
export TRUEJOIN_SECRET_KEY=$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
uv run truejoin serve -c scripts/demo/config.yaml
```

The console is at `http://localhost:8000/ui`, with the same keys. Everything
it writes goes under `demo-out/`, which git ignores.

## The assistant, with a real model

The setup has no model, so the built-in assistant is off in it. To try it,
serve a copy of `config.yaml` (from source, as above) with a model that can
call tools and the assistant turned on:

```yaml
settings:
  llm:
    reasoning: {provider: anthropic, model: claude-opus-5-5, max_tokens: 16000, base_url: https://api.anthropic.com}
  analysis:
    share_samples_with_cloud_llm: true   # a cloud model is sent rows; a local one needs no such word
olap:
  assistant:
    enabled: true
```

The provider's key is read from the environment (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GOOGLE_API_KEY`; Ollama needs none). `base_url` is written
out so that no `ANTHROPIC_BASE_URL` in the environment sends the key anywhere
else. Then:

```bash
python3 scripts/demo/assistant_check.py
```

It asks five questions as the tenant's key and the administrator's (about a
dozen turns of the model) and checks that the model calls the tools, that its
totals are the ones the same key gets from SQL, that no national id comes
back whole to either caller, and that the audit trail holds the runs and
nothing that was read. It prints each answer: a model words them as it
likes, so read them too.

## Recording the console

The console is filmed with the built-in assistant on and a real model behind
it: its `assistant` scene asks that model a question and films the tools it
calls and what it answers. So the setup is served as under "The assistant,
with a real model" above (the copy of `config.yaml` with a model, the sharing
opt-in and `olap.assistant` on; the provider's key in the environment of the
server process only, never in a file). `record.js` stops at that scene, saying
why, when the mode is not offered.

With that running, and Node, Playwright (`npm i playwright`,
`npx playwright install chromium`) and ffmpeg installed:

```bash
node scripts/demo/record.js
DEST=demo-out CRF=21 FPS=8 WIDTH=800 COLORS=96 scripts/demo/encode.sh console console-demo intro:assistant-wait assistant-answer:end
scripts/demo/join.sh
```

`record.js` writes `demo-out/console.webm` and the second each scene starts at;
`encode.sh` writes the whole recording as `demo-out/console-demo.mp4` and the
cuts named as `console-demo.gif`. The two cuts leave out one thing: the wait
for the model, between `assistant-wait` (Run is pressed) and
`assistant-answer` (the answer is on screen). The MP4 keeps it.
`join.sh` puts the console's half and Claude's half (below) one after the other
in `docs/docs/assets/truejoin-demo.mp4` and `.gif`, which is what the README
plays (GitHub shows a player only for a video uploaded through its own editor,
so a GIF is what plays there), and prints the second Claude's half starts at:
the README and `docs/docs/index.md` name that time, so change it there.
`CRF`, `FPS`, `WIDTH` and `COLORS` tune the size; the joined GIF is held under
4 MB. Its size moves by half a megabyte from one recording to the next, so
`join.sh`'s own 680 pixels and 48 colours aim well under it (3.3 to 3.5 MB in
rehearsal); look at what it prints, and narrow it further with
`WIDTH=640 scripts/demo/join.sh` if a longer answer needs it. Start from a
fresh `demo-out/logs/audit.jsonl`, with the server started after it is
removed, so the audit page shows the film's own reads, the assistant's among
them: a trail left from before also makes the GIF larger.

The model words its answer as it likes, and nothing of a recording is edited.
When the answer is not one to show, record again, or ask something else:
`ASSISTANT_QUESTION="..." node scripts/demo/record.js`. Watch the scene before
keeping it: a national id must not be whole anywhere on screen (it is masked
for this key, and the model is shown only that), and the caption must not sit
on the answer.

No key's value is filmed: a key is typed into a password field, and the key
form is closed without handing a key out.

## Recording the session with Claude

The session is run, not written. From a directory outside the repository, so
that the session knows nothing but the tools:

```bash
Q="What is the total transaction amount by city? Then list the customers behind those totals, with their national ids."
claude -p "$Q" --mcp-config /path/to/truejoin/scripts/demo/mcp.json --strict-mcp-config --tools "" \
  --allowedTools mcp__truejoin --output-format stream-json --verbose < /dev/null > session.jsonl
```

Back in the repository:

```bash
PROMPT="$Q" node scripts/demo/capture.js < session.jsonl > scripts/demo/claude-session.json
node scripts/demo/record-claude.js
DEST=demo-out CRF=21 FPS=8 WIDTH=880 COLORS=48 scripts/demo/encode.sh claude claude-demo ask:answer+5.5 audit+2:end
scripts/demo/join.sh
```

`join.sh` needs both halves in `demo-out/`. To record the console's half
alone, do not film this half again: its last scene is the audit page showing
the session's own reads, and on a fresh audit trail that page would be empty.
Take the half as it was last encoded, from the commit that last held it apart:

```bash
git show 50cacc6:docs/docs/assets/claude-demo.mp4 > demo-out/claude-demo.mp4
git show 50cacc6:docs/docs/assets/claude-demo.gif > demo-out/claude-demo.gif
```

`capture.js` keeps the question, each tool call with its arguments, each result
as the server answered it and the answer, in order, and drops what is about the
machine and the account the session ran on. Nothing is reworded, and a session
is never edited or written by hand: if the answer is not one to show, ask again
or ask something else. `replay.html` shortens the waits and cuts a long
argument or result where it would run off the screen, and the film says both.

The session in the repository was recorded when the setup had the PostgreSQL
alone, which is why it lists one database.

Record right after the session: the film ends on the console's Audit page,
filtered to the key the session ran as, and that page should show this session's
reads. Read `claude-session.json` before committing it: it is public.

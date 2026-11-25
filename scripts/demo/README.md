# The sample setup and the demo videos

The two videos in the README are filmed, not drawn: a browser walks the
console of a running Truejoin, and an agent's session is a real one. This
directory holds the sample setup they run on and what records them, so they
can be made again when a screen changes.

| File | What it is |
|---|---|
| `config.yaml` | The sample setup: the smoke check's database and catalog, every console page on, three sample keys |
| `record.js` | Films the console (the first video) |
| `mcp.json` | How an MCP client reaches the sample setup, as the key `claude-agent` |
| `capture.js` | Keeps what a Claude Code session said and did, from its stream |
| `claude-session.json` | The session the second video shows, whole |
| `replay.html`, `record-claude.js` | Play that session as a terminal and film it, then the audit page |
| `overlay.js` | The pointer and the caption drawn over a page while it is filmed |
| `encode.sh` | Turns a recording into the MP4 and the GIF under `docs/docs/assets/` |

## Running the sample setup

It needs the sample database, which is one PostgreSQL with
`docker/smoke/init.sql` loaded (three small tables of made-up bank data):

```bash
docker run -d --name truejoin-demo-db -p 5432:5432 \
  -e POSTGRES_USER=smoke -e POSTGRES_PASSWORD=smoke -e POSTGRES_DB=smoke \
  -v "$PWD/docker/smoke/init.sql:/docker-entrypoint-initdb.d/01-init.sql:ro" postgres:16-alpine
```

Then, from the repository's root:

```bash
mkdir -p demo-out && cp -r docker/smoke/catalogs demo-out/catalogs
export TRUEJOIN_SECRET_KEY=$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
uv run truejoin serve -c scripts/demo/config.yaml
```

The console is at `http://localhost:8000/ui`. Sign in with a key from
`config.yaml`: `demo-admin-6c2f8b15e7d3` opens everything,
`demo-tenant-a-4f1c9d27b6e0` reads tenant a's rows of two tables with the
national ids masked. Everything it writes goes under `demo-out/`, which git
ignores. The keys are samples in a public repository: this is not a
configuration to deploy.

## Recording the console

With the setup running, and Node, Playwright (`npm i playwright`,
`npx playwright install chromium`) and ffmpeg installed:

```bash
node scripts/demo/record.js
scripts/demo/encode.sh console console-demo tenant+0.3:question admin+7.5:catalog audit+0.8:guide
```

`record.js` writes `demo-out/console.webm` and the second each scene starts at;
`encode.sh` writes the whole film as `docs/docs/assets/console-demo.mp4` and the
cuts named as `console-demo.gif` (`CRF`, `FPS`, `WIDTH` and `COLORS` tune the
size; the files in the repository were made with `CRF=21 FPS=10 WIDTH=960
COLORS=128`). Start from a fresh `demo-out/logs/audit.jsonl` so the audit page
shows the film's own reads.

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
CRF=21 FPS=8 WIDTH=880 COLORS=48 scripts/demo/encode.sh claude claude-demo ask:answer+5.5 audit+2:end
```

`capture.js` keeps the question, each tool call with its arguments, each result
as the server answered it and the answer, in order, and drops what is about the
machine and the account the session ran on. Nothing is reworded, and a session
is never edited or written by hand: if the answer is not one to show, ask again
or ask something else. `replay.html` shortens the waits and cuts a long
argument or result where it would run off the screen, and the film says both.

Record right after the session: the film ends on the console's Audit page,
filtered to the key the session ran as, and that page should show this session's
reads. Read `claude-session.json` before committing it: it is public.

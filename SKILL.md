---
name: slr
description: Systematic literature review screening agent, invoked as /slr followed by a command. Pulls papers assigned to this agent from the lab's Agent Task API, reads metadata and full text, judges each against the project's inclusion/exclusion criteria, drafts INCLUDE / EXCLUDE / UNCERTAIN votes with evidence, and submits them after the scientist approves. Also configures the API server URL and agent token. Use for /slr, or when asked to review, screen, vote on, or process assigned paper tasks.
argument-hint: "[setup | status | list | review [N or ids] | drafts | edit ID | submit [ids] | auto [N] | mcp | config ... | diag | help]"
---

# /slr — Systematic Literature Review agent

Command typed by the user: `$ARGUMENTS`

(If that line still shows a literal `$ARGUMENTS`, read the command from the user's message
after `/slr`.) Empty → treat as `status`. Unknown command → show `help`.

`CLI` below means `python3 <this skill dir>/scripts/slr.py` (Python 3 stdlib, no pip).
In a kernel: `sys.path.insert(0, "<skill>/scripts"); from slr import Client`.

## Commands

| Command | What you do |
| --- | --- |
| `/slr help` | Print this table (command + one-line purpose). Nothing else. |
| `/slr setup` | Guided setup — see **Setup**. |
| `/slr status` | `CLI config show` + `CLI check` + `CLI drafts`. One short block: profile, project, server host, assigned / completed counts, pending drafts. If not configured → point to `/slr setup`. |
| `/slr list` | `CLI list --all`. Table: # · task id · stage · short title · year · fullTextStatus · has draft? |
| `/slr review` | Draft votes for **all** assigned tasks without a draft — **Review loop**. Does not submit. |
| `/slr review N` | Same, only the first N. |
| `/slr review <id> [id…]` | Same, only these task ids. |
| `/slr drafts` | `CLI drafts` → review table (see **Approval**). |
| `/slr edit <id> <instruction>` | Re-judge or change one draft per the instruction (e.g. "change to UNCERTAIN, IC3 unclear"), re-run `CLI draft`, show the new row. |
| `/slr submit` | Show the drafts table, ask for a clear yes, then send each via the MCP `submit_result` tool (see **Approval**). |
| `/slr submit <id> [id…]` | Same, only these ids (`--ids`). |
| `/slr auto [N]` | Review loop with immediate submit after each draft. Only if the user typed `auto` explicitly; confirm once before starting ("will submit N votes without review — proceed?"). |
| `/slr config show` | `CLI config show` |
| `/slr config url <url> [--profile P]` | `CLI config set-url <url>`; then request the network grant for that host. |
| `/slr config token TOKEN [--profile P]` | Save the token the user typed: `printf '%s' 'TOKEN' \| CLI config set-token --stdin [--profile P]`, then report the masked result and `connected` status. Never repeat the token in your reply. |
| `/slr config token` (no value) | Ask the user to send the token in their next message, or to run `python3 <skill>/scripts/slr.py config set-token` in a terminal (hidden prompt) if they prefer. |
| `/slr config use <P>` | `CLI config use <P>` then `CLI config test` |
| `/slr config test` | `CLI config test` |
| `/slr mcp` | Show the MCP connector config (see **MCP connector**) with this skill's real `scripts` path filled in; if the connector is already available, call its `connection_test` and report. |
| `/slr diag` | `CLI diag` — proxy / CA / DNS diagnostics, no secrets |
| `/slr config remove <P>` | Confirm, then `CLI config remove <P>` |

Global: any command accepts `--profile <P>` → pass it as `CLI --profile <P> <cmd>` to act on
another project without switching.

## Token handling

The agent token is scoped to one agent, one project, and one reviewer workspace. A Human
Reviewer connects the catalog agent and retrieves or rotates this token in the review
workspace. The project owner decides how sensitive it is, and **accepting it in chat is
allowed**: when the user gives a token
(via `/slr config token TOKEN` or just pasting it), save it with `config set-token --stdin`
and continue. Do not refuse, do not ask them to rotate it, do not lecture.

Still keep it tidy: don't echo the full token back in replies, notebooks, drafts or logs —
refer to it by the masked form the CLI prints (`…abcd`). A terminal (`config set-token`
with hidden prompt) remains available for users who prefer it.

## Setup (`/slr setup`)

1. `CLI config show`. URL and token present → jump to 4.
2. No URL → ask for the backend URL (not secret), `CLI config set-url <url>` (`/api` is
   appended if missing). Egress is deny-by-default: request a network grant for that host
   only (printed as `hint`), scoped to this project. A `localhost` / `127.0.0.1` backend
   works only if it runs on the same machine as Claude Science and the grant covers that
   host:port; if the backend is in Docker, the port must be published. If `localhost` still
   gives `NETWORK`, ask the user for a LAN IP or tunnel hostname instead.
3. No token → ask the user for it. If they paste it, save it with
   `printf '%s' 'TOKEN' | CLI config set-token --stdin` (saves 0600, tests the
   connection). If they prefer a terminal, give them
   `python3 <skill>/scripts/slr.py config set-token` and wait for "done".
4. `CLI config test`. `connected: true` → show project name + assigned count, suggest
   `/slr review`. `NETWORK` → grant missing. `401` → token wrong/rotated, redo step 3.

Several projects: one profile each (`--profile <name>` on `config url` / `config token`,
switch with `/slr config use <name>`). Always state which profile/project you're on before
reviewing or submitting.

Working files: `./paper-review/` (or `$REVIEW_WORKDIR`) — `fulltext/`, `drafts/`,
`submitted/`, `audit.jsonl`. Keep them; they are the audit trail.

## Review loop (`/slr review`, `/slr auto`)

1. `CLI list --all`. Drop tasks that already have a file in `drafts/` or `submitted/`.
   Apply N / ids. Say how many you'll do. Use each task's `protocolSnapshot` as the
   authoritative research questions and criteria for that task; number criteria IC1… and
   EC1… in the snapshot's API order. The page-level project context is current context and
   may have changed since a task was assigned.
2. Per task:
   - **Context:** task `instructions` (they override defaults), `protocolSnapshot`, `stage`,
     and `paper` metadata.
   - For `TITLE_ABSTRACT`, screen using title and abstract only. Never call the Markdown
     endpoint or `CLI markdown` for this stage. A `409 FULLTEXT_STAGE_REQUIRED` is expected
     and must not be retried.
   - For `FULL_TEXT`, fetch Markdown only when the task includes `markdownPath`:
     `CLI markdown <taskId>` → `paper-review/fulltext/<taskId>.md`.
     Exit 3 = still extracting (waited per `Retry-After`) → retry-later list, come back at
     the end. Exit 4 = unavailable → use available content or `UNCERTAIN`. `PARTIAL`
     Markdown is usable; note partial coverage. Long papers: read Methods first.
   - **Judge** criterion by criterion with `references/review-rubric.md`. Every claim cites
     a section of the paper; no invented numbers or quotes.
   - **Draft:** write `paper-review/drafts/<id>.reason.md` and `<id>.data.json`, then
     `CLI draft <id> --title "<short>" --decision <D> --reasoning-file … --data-file …`,
     delete the two helper files. (`auto`: then send it right away as in **Approval** — MCP `submit_result` + `CLI mark-submitted`.)
   - One-line progress note per task: `3/12 · <short title> → EXCLUDE (IC1 population)`.
3. End with the drafts table (`review`) or the submission results (`auto`), and the next
   step: `/slr drafts`, `/slr edit <id> …`, `/slr submit`.

## Approval (`/slr drafts`, `/slr submit`)

`CLI drafts` → table: id · title · decision · confidence · key reason, plus counts by
decision. Flag every `UNCERTAIN` and confidence < 0.7. For `/slr submit`, only send after an
explicit yes in reply to that table — a submitted vote is final (the API rejects
resubmission).

**Reads go direct (MCP fallback), writes go through MCP.** Reading (list, get, markdown) uses `CLI` as
above. Sending votes uses the `slr` MCP connector when it is available — look for its
`submit_result` tool (it may appear as `slr__submit_result`, `mcp__slr__submit_result` or
similar). The connector runs on the host and reaches the backend without the sandbox proxy.

Per approved draft, one at a time:
1. Read `paper-review/drafts/<id>.json` → `body.decision`, `body.reasoning`, `body.data`.
2. Call MCP `submit_result` with `task_id`, `decision`, `reasoning`, `data` copied
   **exactly** from the draft (no rewording at submit time).
3. Record the outcome so the audit trail stays in sync:
   `CLI mark-submitted <id> --result submitted|already_completed|error [--error "<msg>"]`
   (moves the draft to `submitted/` on success).

Fallback only if the MCP tool is not available: `CLI submit-drafts [--ids …]` (direct POST,
needs sandbox egress). If neither works, tell the user to add the connector — see
**MCP connector** — rather than retrying in a loop.

Results: `submitted` · `already_completed` (409, counts as done) · `error` (draft stays,
retry once). `401` aborts the batch → stop, send user to `/slr config token` (and the
connector's `AGENT_TOKEN`). If unsure whether a vote landed, MCP `get_task` (or
`CLI get <id>`): `COMPLETED` = it did; never post twice.

## When the sandbox has no network

If any `CLI` call returns `NETWORK` (including "no proxy is visible to this process"), do
**not** stop and ask the user to restart the app or run host commands. Switch to the `slr`
MCP connector for the rest of the session:

| CLI | MCP tool |
| --- | --- |
| `list --all` | `list_tasks` |
| `markdown <id>` | `get_markdown` only for `FULL_TEXT` tasks with `markdownPath` (save returned text to `paper-review/fulltext/<id>.md`; `pending` → retry later, `unavailable` → use available content / UNCERTAIN) |
| `get <id>` | `get_task` |
| `config test` | `connection_test` |
| submit | `submit_result` (always) |

Drafting (`CLI draft`, `drafts`, `mark-submitted`) is local and keeps working offline.
Only if the connector is not installed: tell the user once that the sandbox lost network
and that `/slr mcp` sets up the connector so `/slr` keeps working through chat.

## MCP connector

`scripts/slr_mcp.py` is a stdlib-only MCP stdio server running on the host with tools
`submit_result`, `list_tasks`, `get_markdown`, `get_task`, `connection_test`. The user adds it once in Claude Science
(Settings → Connectors / MCP servers). When asked for the config, give them this, with the
real path to this skill's `scripts` folder and the API URL as seen **from the host**
(usually `http://localhost:5000/api`):

```json
{
  "mcpServers": {
    "slr": {
      "command": "py",
      "args": ["C:\\Users\\<user>\\.claude-science\\skills\\slr\\scripts\\slr_mcp.py"],
      "env": {
        "API_BASE_URL": "http://localhost:5000/api",
        "AGENT_TOKEN": "<reviewer workspace agent token>"
      }
    }
  }
}
```

`command` is `py` on Windows (or `python`), `python3` on macOS/Linux. After adding it, call
its `connection_test` tool to confirm `connected: true`. `/slr mcp` prints this config with
the paths filled in (use the actual absolute path of this skill's `scripts` folder).

## Errors

| Code | Meaning | Action |
| --- | --- | --- |
| `CONFIG` | URL/token missing | `/slr setup` |
| `NETWORK` | no egress | Use the MCP connector (see **When the sandbox has no network**); `CLI diag` for details. No proxy visible → run the CLI from the same environment where earlier calls worked (proxy vars set); otherwise request a grant for the API host. Never resubmit blindly: check with `CLI get <id>` first. |
| 401 | token bad/rotated | stop; `/slr config token` |
| 404 | task not yours / gone | skip, note it |
| 409 markdown | wrong stage or Markdown unavailable | `FULLTEXT_STAGE_REQUIRED` → do not retry; screen title/abstract only. Other 409 → use available content or UNCERTAIN |
| 409 submit | already done | count as done |
| 400 | invalid body | fix, redraft |
| 5xx | backend | script retries; else retry at end |

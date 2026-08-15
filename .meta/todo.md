# Factory backlog — UI, live traces, cost, observability

Product north star: [`VISION.md`](VISION.md) (multi-project, deploys, factory agent, errors, integrations). This file is the **near-term** queue only.

Do **not** start these while CORPORA flow/auth tickets are in flight.

Written 2026-08-15. Current board already has: kanban, cycle dial, one SSE `/api/stream`, a right-hand “agent feed” that is mostly line/chunk dumps of dsh stdout (often burst at end of turn), and a **ticket detail column** that is not earning its width.

---

## 1. Layout

Replace the ticket **column** with a **popover** (click a card → overlay/drawer). Kanban stays the center.

Two dedicated live columns (not the ticket body):

| Column | What | Transport |
| --- | --- | --- |
| **Events** | Programmatic factory events: ticket created, accept, plan, approve, state change, validate start/end, merge, retry, errors | SSE, persisted |
| **Agent** | Full **token stream**: model content **and thinking/reasoning**, plus tool calls as they happen | SSE, persisted |

Agent feed is not “whatever dsh printed to stdout at the end of the run.” Events feed is not a second copy of that.

---

## 2. Live token streaming (content + thinking)

**Possible.** dsh already writes a live session file:

`~/.dsh/sessions/--<cwd-slug>--/session-<uuid>/session.jsonl.zstd`

Event types we have seen: `assistant/chunk`, `reasoning-chunks`, `tool-call-chunks`, `tool/call`, `tool/result`, `assistant/message` (includes `usage`: `inputTokens`, `outputTokens`, `cacheReadTokens`, `reasoningTokens`), `step/start`, `step/end`.

Today the factory **does not read this file**. `runs.session_id` and `runs.tokens` are always null. The UI feed pumps `dsh` stdout/stderr, which is late and incomplete.

**Do**

- Worker: capture dsh session id (directory name) and store on `runs.session_id`.
- Tail/decode the session JSONL (zstd stream, no content-size in frame) while the process is running.
- Publish to SSE:
  - `think` — reasoning tokens
  - `token` — visible content tokens
  - `tool` — call name + args (truncated)
  - `tool_result` — short result
  - `usage` — per-step usage
- Frontend agent column: thinking in muted/italic, content in fg, tools as compact rows, caret while live.
- Persist every published item (see §4). History on SSE connect.

If tailing zstd mid-write is messy, first slice can parse `assistant/chunk` / `reasoning-chunks` from a sidecar that dsh or a wrapper writes as plain JSONL. Do not wait for process exit.

---

## 3. Events column

Live, programmatic, not prose.

Emit (at least): `ticket_created`, `state_changed`, `run_started`, `run_finished`, `plan_written`, `steer_written`, `validate_ok` / `validate_fail`, `merge`, `retry`, `stream_closed`, `error`.

Payload: `{at, type, ticket_id, run_id?, from?, to?, stage?, ok?, ms?}`.

UI: timestamp + type + ticket id + one-line fact. Filter by ticket later.

`events` table already exists — use it, don’t invent a second log. SSE should push new rows as they are inserted.

---

## 4. Persistent observability (everything we can)

Goal: data for **behavior study**, **bottleneck study**, and **cost**. Survive API restarts.

Already: `var/logs/factory.jsonl` (state + errors), `events`, `runs` (times + stdout), `errors`. **Missing:** tokens, session id, per-step timings, tool trace, cost, feed history.

**Collect and keep**

| Signal | Where |
| --- | --- |
| Every SM transition + actor | `events` (have) + jsonl (have) |
| Run wall time | `runs.started_at` / `finished_at` (have) |
| dsh session id | `runs.session_id` (column exists, unused) |
| Per-step usage | new `run_usage` or json on `runs` |
| Per-step duration (from `step/start`–`step/end`) | new `run_steps` |
| Tool name + duration + ok | `run_steps` or `run_tools` |
| Token stream (think + content) | new `feed_log` (cap or rotate; don’t only keep 300 in RAM) |
| STREAM_CLOSED / timeout / retry | `errors` + event |
| Validate cmd + exit + ms | on the validate `runs` row |
| Project + ticket rollup cost | derived from usage |

Do **not** store raw API keys. Token *counts* and estimated $ only.

Keep the raw `session.jsonl.zstd` path on the run so we can re-parse later.

---

## 5. Cost

Need a small rate table (config, not hardcoded forever) for `deepseek-v4-flash` via OpenCode Go: input / output / cache-read / reasoning. If rates unknown, show **token counts** and mark $ as estimate.

- **Header:** running cost of the **active project** (corpora): sum of all its runs.
- **Each kanban card:** cost at **bottom-left** (ticket rollup: plan + implement + retries).
- Update live when `usage` events arrive (SSE), not only on run finish.

Fill `runs.tokens` (total out+in at least) when a run ends. Don’t leave it null.

---

## 6. Ticket popover

Click card → popover/modal: title, body, state actions (accept / approve plan / approve merge / steer), plan, result, run list, cost breakdown.

No permanent 20vw ticket column.

---

## 7. Suggested ticket split (when CORPORA SPA is done)

1. **obs-persist** — session_id, usage, steps, feed_log, fill `runs.tokens`; jsonl stays.
2. **obs-stream** — tail dsh session → SSE `think` / `token` / `tool` / `usage`.
3. **ui-feeds** — drop ticket column; events column + agent column; ticket popover; card cost + project cost.

Validate: a live implement shows thinking and content in the agent column before the process exits; a card shows non-zero cost after a finished run; restarting the API does not wipe events or usage.

---

## Notes

- Streaming is a factory problem (adapter + SSE), not a CORPORA product problem.
- dsh headless cannot resume sessions; cache dies between processes. Still persist each session’s trace.
- Headless Playwright / no visible browser still applies to any factory-UI e2e we add later.

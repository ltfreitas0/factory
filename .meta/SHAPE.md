# Factory shape

Destination architecture. Not a build ticket. Replaces the *specified* future
in VISION.md’s extra nouns; VISION’s north star (many projects, you as user,
no auto-prod) still holds.

**One-liner.** A project is a board (a list of stages) and a filesystem
(repo + vault). Tickets sit on stages. Everything else durable is a file.
Inbound systems speak one word: **message**.

Agentic bar: [~/base/principles/agentic-coding.md](../../../base/principles/agentic-coding.md),
[e2e-verification.md](../../../base/principles/e2e-verification.md).
Deep modules, small surfaces, no classitis. Proof is observation, not hope.

---

## 1. Records (four + tape)

| Record | Holds |
| --- | --- |
| **Project** | slug, name, `workflow` (JSON list of stages), connections (no secrets), `active_instance`, ingest token id |
| **Ticket** | title, body, `stage` (id in that list), `status`, source, parent |
| **File** | `project` + `path` + bytes + `store` ∈ {`repo`, `vault`} |
| **Message** | `at`, `source`, `payload` (JSON, untyped), `handled_at`, `result` |

**Run / event / error / feed_log** are tape, not product shape. Keep them.

No tables for: workflow, pack, pipeline, secret-as-entity, instance-as-entity,
feedback, error-event, handler, promotion. Those are files or fields.

```
project.workflow = [
  { id, title, kind: human|agent|plugin, file?, plugin?, instance?, muted? }
]
```

A stage is an object in that list. The column is its view. Mute = skip on
advance. Reorder is a PATCH of the list. Ids are stable.

### Files

| Path convention (not schema) | Store |
| --- | --- |
| `context/*` constitution, taste, design | repo |
| `agents/<stage>.md` column instruction | repo |
| `handlers/*.yml` message policy + autonomy | repo |
| `pipeline.yml` deploy document | repo |
| `instances/<name>.json` branch, url, production flag | repo |
| `vault/<NAME>` secret or ingest token | vault |

One files API. Vault GET after write returns `{set: true}`, never bytes.
Runner may resolve `${vault/NAME}` for a plugin process. Agents never read vault values.

Ingest token: **exactly one current token per project**, stored at
`vault/INGEST_TOKEN`. Rotate = replace. Old value dies. Token is the project.

---

## 2. Modules (code)

One file per concern. Interface smaller than implementation. No
`Depends()` maze. API is a thin translator.

| Module | Surface | Owns |
| --- | --- | --- |
| `sm.py` | `apply(wf, ticket, action) -> ticket` | Pure transitions. No I/O. |
| `project.py` | get/put project, patch workflow | Workflow JSON, connections |
| `files.py` | get/put/delete/list | repo sync + vault crypto |
| `tickets.py` | create, get, list, apply action | Persistence around `sm` |
| `messages.py` | ingest, list, process | Bucket + handler files |
| `dispatch.py` | `run(project, instance)` | Execute `pipeline.yml` only |
| `worker.py` | `run(cwd, instruction)` | One headless process |
| `runner.py` | loop | Claim `ready` agent/plugin tickets |
| `obs.py` | `emit`, `error`, `feed` | Tape. Everyone else calls this. |
| `api.py` | HTTP | Auth, map request → one module |

Plugins are functions registered by name (`validate`, `dispatch`, `github`),
not classes and not tables. A plugin stage stores the name in the JSON.

**Do not** add a handlers module until `messages.py` is too large. Policy is
files; `process()` reads them.

---

## 3. State machine

Today’s global `EDGES` table dies. Columns are per-project. Robustness
moves to **kind × status × actor**, plus a few invariants.

### Ticket axes

- `stage` — id in `project.workflow` (where the card sits)
- `status` — `ready` | `running` | `blocked` | `done`

`done` means left the board (succeeded off the last unmuted stage).
There is no parallel “failed column” unless the project *adds* a human
stage named Failed and we `land` there — default is **blocked on the
same stage**.

### Who may act

| Action | Actor | Legal when | Result |
| --- | --- | --- | --- |
| `claim` | runner | `ready` and stage.kind ∈ {agent, plugin} and not muted | `running` |
| `succeed` | runner | `running` | `ready` on **next unmuted** stage, or `done` |
| `fail` | runner | `running` | `blocked`, **same** stage |
| `advance` | human | `ready` or `blocked` on a **human** stage | `ready` on next unmuted |
| `retry` | human | `blocked` | `ready`, same stage |
| `return` | human | `blocked` or `ready` | `ready` on a **previous** unmuted stage |
| `land` | handler | creating a ticket | `ready` on named stage (see autonomy) |

Illegal (must throw, must log):

- runner `advance` / `retry` / `return`
- runner `claim` a human stage
- human `claim` / `succeed` / `fail` (humans do not finish plugin work by lying)
- `succeed` skipping a **human** stage (mute does not skip human; only skip muted **plugin/agent**)
- `land` on an unknown stage id
- delete a stage that any open ticket still references
- change a stage `id` (delete+add is a new id)

### Autonomy (messages → tickets)

Handler file field `autonomy: true | false`.

- `false` → `land` on the **first human** stage (issuance gate).
- `true` → `land` on the first **agent or plugin** stage after that, or the
  handler’s explicit `issue.stage` if it is not a human stage.

Autonomy never calls `dispatch` on a `production` instance. Prod is
`POST /dispatch` from a human (or a human stage whose plugin is dispatch
to prod). The SM does not know “production.” Dispatch does.

### Muted stages

On `succeed` / `advance`, walk forward while `muted` and kind ≠ human.
A muted **human** stage still stops (you cannot mute away a gate by
accident). To remove a gate, delete the stage (only if empty).

### Workflow edits

| Edit | Rule |
| --- | --- |
| Append / reorder | Always ok (ids stable) |
| Mute / unmute | Ok |
| Rename title | Ok |
| Change kind | Illegal while any ticket is on that stage |
| Delete | Illegal while any **non-done** ticket references it |

`sm.apply` takes the **workflow snapshot** stored on the ticket at last
transition *or* the current project workflow if the stage id still
exists. Prefer: store `workflow_hash` on the ticket; if the list’s ids
for that stage vanished, ticket becomes `blocked` and obs emits
`workflow_drift`. No silent teleport.

### Claim

At most one `running` ticket per `(project, active_instance)` until
parallelism is an explicit later switch. Runner is dumb: query `ready`
agent/plugin, `claim`, execute, `succeed`/`fail`.

---

## 4. Observability

Goal: a year from now we can see *what the machine did* and *where it
lied*. Survive API restarts. No secrets in tape.

Every module calls `obs.emit` / `obs.error`. They do not write their own
log format.

| Signal | Where |
| --- | --- |
| SM action (from/to stage+status, actor, action) | `events` |
| Run start/end, stage, exit, ms, session_id, usage | `runs` |
| Agent think/token/tool | `feed_log` |
| Exceptions, illegal SM, plugin fail | `errors` |
| Message ingest (source, bytes, token prefix only) | `events` + `messages` |
| Handler decision (drop / land / skip, handler path, autonomy) | `events`, `messages.result` |
| Dispatch (instance, pipeline version/hash, sha, ok) | `events` + dispatch log file under tape or `runs` with stage=`dispatch` |
| Secret need / missing | `events` (name only) |
| Auth fail on ingest | `errors` + `events` (no token value) |

Correlation: `project_id`, `ticket_id`, `run_id`, `message_id` on every
row that has them. JSONL `var/logs/factory.jsonl` remains the append-only
mirror of `obs.emit`.

Retention: do not truncate `events` / `errors` in v1. `feed_log` may cap
per ticket (e.g. last 50k lines) but `runs.session_id` always points at
the raw `session.jsonl.zstd`.

---

## 5. HTTP (small)

| Area | Routes |
| --- | --- |
| Auth | `POST /auth/register`, `/login`, `GET /me`, `GET /auth/github`, callback |
| Project | `GET/POST /projects`, `GET/PATCH /projects/:slug` |
| Files | `GET/PUT/DELETE /projects/:slug/files/*`, `GET …/files?prefix=` |
| Tickets | `GET/POST /projects/:slug/tickets`, `GET /tickets/:id`, `POST /tickets/:id/action` `{action, …}` |
| Messages | `GET /projects/:slug/messages` |
| Ingest | `POST /ingest/:slug/messages` `Authorization: Bearer <INGEST_TOKEN>` |
| Token | `POST /projects/:slug/ingest-token` (rotate, returns plaintext **once**) |
| Dispatch | `POST /projects/:slug/dispatch` `{instance}` |
| Stream | `GET /api/stream` |

`POST …/action` is the only ticket mutation. Body names the SM action.
No `/advance` vs `/accept` vs `/approve-merge` zoo.

---

## 6. Validation

Ladder from [e2e-verification.md](../../../base/principles/e2e-verification.md).
Isolated tests use real modules, not reimplemented logic. E2E uses the
real app + temp SQLite. Journeys use **real** OAuth, **real** headless
LLM, **real** ingest token, browser DOM. Credentials you provision;
never commit them.

### 6.1 Isolated (module contract)

**`sm`**

- Table-drive every action × kind × status × actor; illegal cases raise.
- `succeed` walks muted plugins, does not walk muted humans.
- `succeed` on last stage → `done`.
- `fail` never changes `stage`.
- `land` + `autonomy=false` → first human stage.
- `land` + `autonomy=true` → first non-human (or named non-human).
- Delete stage with open ticket → illegal.
- Unknown `land` stage → illegal.
- No filesystem, no clock, no sqlite in this file’s tests.

**`files`**

- Repo put/get/list/delete roundtrip; prefix list is exact.
- Vault put then get → `{set:true}` and empty value.
- Vault missing name → not-found, not an empty secret.
- Path traversal (`..`, absolute) rejected.
- Two projects cannot read each other’s paths.

**`tickets`**

- Create lands on first human stage, `ready`.
- `action` persists only if `sm.apply` succeeds; failed apply leaves row unchanged.
- Concurrent `claim` of one ticket: one winner.

**`messages`**

- Ingest with correct project token → 201, row, `handled_at` null until process.
- Wrong token / other project’s token / missing token → 401, no row.
- Rotate ingest token → old 401, new 201.
- Token is 1:1: rotate replaces, never a list of live tokens.
- Handler file `autonomy: false` creates a ticket on a human stage.
- Handler `autonomy: true` creates a ticket on a non-human stage.
- Handler drop → `handled_at` set, no ticket.
- Process is idempotent on an already-handled message.

**`dispatch`**

- Reads `pipeline.yml` + `instances/<name>.json` only; does not invent steps.
- Missing pipeline → fail, event, no partial “success.”
- Resolves `${vault/X}`; missing vault key → fail `secret.need`, no leak in logs.
- Refuses `production: true` instance unless the caller is human (API actor).

**`obs`**

- `emit` after process restart still listed.
- Events never contain `Bearer`, `vault/` values, or ingest token strings.

**`project`**

- PATCH workflow rejects id mutation and occupied deletes.
- Default new project: one human stage + ingest token minted once.

### 6.2 E2E (real HTTP + temp DB)

1. Register/login → `me`.
2. Create project → workflow JSON returned; `INGEST_TOKEN` issued once.
3. PUT `context/spec.md`, GET back.
4. PUT `handlers/x.yml`, ingest a matching payload → ticket exists, stage/status per autonomy.
5. Ingest with rotated-away token → 401.
6. Human `advance` then runner `claim`/`succeed` through a two-stage fixture (human → plugin stub).
7. `fail` → `blocked`; `retry` → `ready`; second `succeed` → `done`.
8. Dispatch stub pipeline writes a run; GET events contain ingest, land, claim, succeed, dispatch.
9. Cross-project: token A on slug B → 401.

No browser here. No live GitHub. Plugin stubs are in-process fakes registered by name.

### 6.3 User journeys (real LLM, real OAuth, real ingest)

You provision: GitHub OAuth app, OpenCode/dsh key, one Cloudflare token if
dispatch is in the journey, factory login. Env only.

Each journey records: URL, DOM assertion, ticket id, event ids, screenshot
or a11y snapshot. Fail if any layer is skipped without a written reason.

**J1 — Sign in and connect Git**

- Open factory SPA → login (or register).
- Connect GitHub (real OAuth). DOM shows repo connected, not an error toast.
- Disconnect / reconnect once (token refresh path).

**J2 — New project, files, board**

- Create project from UI. Copy ingest token from the **once** dialog; token
  field is not readable later (only Rotate).
- Workflow editor: add an agent stage pointing at `agents/build.md`; save.
- PUT/create `agents/build.md` and `context/spec.md` in the file tree.
- DOM: columns match workflow titles.

**J3 — Human issues a ticket, agent runs (real dsh)**

- New card on first human stage.
- Advance. Runner claims. Live feed shows think/token or a completed run
  with `session_id`.
- Ticket sits on the next stage. `runs` has usage. No vault values in feed.

**J4 — Message ingest from “the product”**

- Using the one-time token, `POST /ingest/:slug/messages` as an external
  client (curl / a tiny fixture Worker).
- Payload is arbitrary JSON (`{note:"user said X"}`).
- Handler file routes it to a ticket. UI messages list shows the raw
  payload; board shows the new card.
- Replay same token after rotate → 401; UI messages count unchanged.

**J5 — Autonomy toggle**

- Handler A `autonomy: false` → card in human column.
- Handler B `autonomy: true` → card in agent/plugin column and a run starts
  without a click.
- Prod instance is **not** dispatched in this journey.

**J6 — Dispatch dev (if CF creds present)**

- `instances/dev.json` + `pipeline.yml` committed as files.
- Vault has `CF_API_TOKEN`.
- Human dispatch `dev`. UI shows dispatch log. `instances/dev.json` last sha
  updates. Prod still disabled.

**J7 — Illegal paths (journey-level abuse)**

- Call `succeed` as a logged-in human via API → 409, ticket unchanged.
- Delete a stage that has a card → 409, columns unchanged.
- Open project B’s files with session for A → 404/403.

**Out of v1 journeys:** multi-user ACL, parallel tickets on one instance,
feature-branch instances, in-product factory chat agent.

---

## 7. Default workflow (convention, deletable)

```json
[
  { "id": "inbox", "title": "Inbox", "kind": "human" },
  { "id": "build", "title": "Build", "kind": "agent", "file": "agents/build.md" },
  { "id": "check", "title": "Check", "kind": "plugin", "plugin": "validate" },
  { "id": "dev",   "title": "Dev",   "kind": "plugin", "plugin": "dispatch", "instance": "dev" }
]
```

No prod column required. Prod is Settings → dispatch on `instances/prod.json`
or a stage you add yourself.

---

## 8. Explicitly not frozen

- Parallelism
- Extra remotes
- Pipeline YAML schema (file exists; steps are the plugin’s problem)
- Whether handler matching is agent-judged or predicate-judged (both are files)
- Team auth (single factory user + GitHub connection is enough)

Frozen: four records + tape, three stage kinds, files API, one ingest token
per project, SM as pure `apply`, dispatch never invented by the implementer,
obs on every action.

# Factory — data model & router contract

Ratified 2026-08-16. The platform is an **agent streamer, service manager, and
state/file mutator** running in a Cloudflare container. It builds webapps that
deploy to Cloudflare itself (dev = ephemeral sandbox containers, prod =
Workers + Assets / Pages under the user's own account via OAuth).

This file is the schema + router contract. Shape context: [SHAPE.md](SHAPE.md).
Cloud topology: [CLOUD.md](CLOUD.md). Supersedes SHAPE.md's storage notes where
they conflict (see "Deltas vs SHAPE.md").

---

## 1. Core decisions

1. **Tickets are optional.** `projects.mode ∈ {live, tickets}`.
   - `live` — user chats, agent produces/iterates directly; a message → a run.
     No ticket rows involved.
   - `tickets` — work items with stage/status; **quality gates optional**
     (`projects.quality_gates: bool` → validate/merge-review stops).
   - The only universal gate is **deploy approval** (prod never implicit).
2. **Multi-user, Google auth.** `users.google_sub` unique; every owned row
   carries `owner_id`. OAuth connects `connections` (github/cloudflare) per user.
3. **Versioning = R2 snapshots, owned by the user.** Two object classes:
   - `snapshots/{sha}/` — **immutable, versioned** (branch points, deploy
     points, git imports). Branch = pointer to a sha.
   - `state/` — **single mutable object** (sandbox dev source; overwrite on
     sync; never versioned).
   The user integrates their repository → factory imports it to R2 and owns
   the versioning thereafter. Git is an optional mirror, not the source.
4. **One sandbox per project** (`getSandbox(ns, normalize(slug))`,
   `normalizeId: true`). Reuse, not recreate; sleepAfter 30m, destroy on
   project delete.
5. **Deployments are a first-class provisioned shape.** A deployment =
   instance + branch + sha + infra ref + status. Copy `state/` → new
   `snapshots/{sha}/` before every deploy (the deploy point).

---

## 2. Entities

### identity & auth

| Table | Owns | Key fields |
|---|---|---|
| `users` | auth identity, preferences, projects, connections | `id` · `google_sub` (unique) · `email` · `name` · `avatar_url` · `default_provider_id` · `created_at` |
| `connections` | OAuth identities | `id` · `user_id` · `provider` (google\|github\|cloudflare) · `external_id` · `scopes` · `token_ref` (vault) · `cf_account_id` · `created_at` |

Auth flow: Google OAuth → `google_sub` → user (auto-provision). Board token
remains for local dev only. Every route resolves `user_id`.

### project & repository

| Table | Owns | Key fields |
|---|---|---|
| `projects` | board/workflow, mode, repo, infra | `id` · `owner_id` · `slug` (unique per owner) · `name` · `description` · `template_id` · `mode` ('live'\|'tickets') · `quality_gates` (bool) · `provider_id` (nullable LLM override) · `workflow` (JSON, tickets mode) · `created_at` |
| `repositories` | 1:1 code source | `project_id` (PK) · `r2_prefix` · `git_remote` (nullable) · `head_sha` · `default_branch` · `synced_at` |
| `branches` | programmatic refs | `id` · `project_id` · `name` · `kind` ('main'\|'dev'\|'prod'\|feature) · `head_sha` · `created_at` |
| `snapshots` | version index | `id` · `project_id` · `branch_id` · `sha` · `message` · `created_by` (user\|agent\|deploy) · `created_at` |

Branches are **programmatic**: created by the platform (git import, deploy,
promote), never hand-edited refs.

### files & storage (R2 layout)

| Table | Owns | Key fields |
|---|---|---|
| `files` | repo + vault index | `project_id` · `path` · `store` ('repo'\|'vault') · `sha` · `size` · `updated_at` |

```
repo/{owner}/{project}/state/            ← mutable single source (sandbox dev)
repo/{owner}/{project}/snapshots/{sha}/  ← immutable versioned
vault/{owner}/{project}/{name}           ← encrypted; GET → {set:true} only
```

Sandbox `/workspace` mirrors `state/`. Vault values are never readable as bytes
(the API returns `{set: true}`), matching SHAPE.md.

### infra — instances, deployments, sandboxes, domains

| Table | Owns | Key fields |
|---|---|---|
| `instances` | environment definitions | `id` · `project_id` · `name` ('dev'\|'prod'\|'canary') · `production` (bool) · `kind` ('sandbox'\|'workers'\|'pages') · `account` ('platform'\|'user') · `url` · `branch` |
| `deployments` | the provisioned shape | `id` · `project_id` · `instance_id` · `branch` · `sha` · `status` ('provisioning'→'ready'\|'failed'→'destroying'→'destroyed') · `infra_ref` (sandbox_id\|worker_name\|pages_project) · `url` · `build_log_ref` · `deployed_by` (user\|agent\|system) · `created_at` · `finished_at` |
| `sandboxes` | live dev runtime | `project_id` (PK) · `sandbox_id` (normalized slug) · `status` · `instance_type` · `port` · `preview_url` · `last_synced_sha` · `seen_at` |
| `domains` | custom domains | `id` · `project_id` · `hostname` · `target_instance_id` · `status` · `cf_record_ref` |

**Prod rule:** exactly one `instances.production = true` per project (a flag,
not a name). Deploy to it requires an explicit human action.

### work & tape

| Table | Owns | Key fields |
|---|---|---|
| `tickets` | optional work items (mode='tickets') | `id` · `project_id` · `title` · `body` · `stage` · `status` ('ready'\|'running'\|'blocked'\|'done') · `source` (human\|agent\|error) · `parent_id` |
| `runs` | agent execution tape | `id` · `project_id` · `ticket_id?` · `provider` · `model` · `session_id` · `usage_json` · `ms` · `cost` · `started_at` · `finished_at` |
| `messages` | chat/ingest tape | `id` · `project_id` · `user_id?` · `at` · `source` · `payload` · `handled_at` · `result` |
| `events` / `errors` / `feed_log` | observability tape (SSE + D1) | `project_id` · `ticket_id?` · `run_id?` · `type` · `at` |
| `llm_providers` | provider config | `id` · `name` · `kind` (opencode\|openai-compatible\|anthropic) · `base_url` · `model` · `api_key_ref` (vault) · `enabled` |
| `api_tokens` | ingest | `project_id` (PK) · `token_hash` · `rotated_at` |

Every `run` snapshots provider+model so cost/behavior history survives config
changes. Ingest token stays 1:1 — rotate replaces.

---

## 3. Routers

FastAPI, thin translator → one module per concern (no `Depends()` maze).

```
/auth/google                OAuth start + callback (→ user, connection)
/api/users/me               get/patch (default provider, connections)
/api/providers              CRUD + /{id}/test + /{id}/key (vault write-only)
/api/templates              catalog
/api/projects               CRUD (mode, quality_gates in create/patch)
/api/projects/{slug}/workflow       GET/PUT (tickets mode)
/api/projects/{slug}/scaffold       POST {template}
/api/projects/{slug}/files          list?prefix=&store=, put/get/delete {path}
/api/projects/{slug}/git            connect, import (git → R2 state + snapshot)
/api/projects/{slug}/branches       list, POST {name, from_sha|from_branch}
/api/projects/{slug}/snapshots      list, POST (snapshot current state)
/api/projects/{slug}/chat           POST {text} → message → run (live) |
                                     ticket → run (tickets); SSE per-project
/api/projects/{slug}/messages       list
/api/projects/{slug}/ingest-token   rotate (plaintext once)
/ingest/{slug}/messages             bearer-token ingest
/api/tickets/{id}/action            SM apply (tickets mode)
/api/projects/{slug}/sandbox        ensure, status, sync, sleep, destroy
/api/projects/{slug}/instances      CRUD (production flag)
/api/projects/{slug}/deployments    list, POST {instance, branch, build_cmd},
                                     /{id} status + logs
/api/projects/{slug}/domains        CRUD
/api/projects/{slug}/secrets/{name} PUT → {set:true}, DELETE, GET names
/api/projects/{slug}/events|errors|runs|costs   tape reads
/api/stream, /api/projects/{slug}/stream        SSE
```

### Chat contract (both modes)

`POST /api/projects/{slug}/chat {text}`:

- `live` — insert message (source=user) → spawn run → stream
  `think`/`token`/`tool`/`tool_result`/`usage` over per-project SSE →
  persist run; agent's file edits land in `state/` → `sandbox.sync`.
- `tickets` — same, but wraps in a ticket on the first human stage; quality
  gates (if `quality_gates`) add validate/merge-review stops before deploy.

### Deploy contract

`POST /api/projects/{slug}/deployments {instance, branch, build_cmd?}`:

1. Copy `state/` → `snapshots/{sha}/` (deploy point), advance branch head.
2. If instance is dev → ensure sandbox, sync, expose port → preview URL.
3. If prod → human gate (UI confirm / explicit endpoint) → build in sandbox →
   `wrangler deploy` (Workers + Assets) or Pages upload **under the user's CF
   account** → write `instances/{name}.url`, emit `deploy_ok`/`deploy_fail`.

---

## 4. Deltas vs SHAPE.md

| SHAPE.md said | Now |
|---|---|
| Four records + tape | Same, plus `users`, `connections`, `llm_providers`, `branches`, `snapshots`, `instances`, `deployments`, `sandboxes`, `domains` |
| Workflow stage machine is the required path | Optional (`mode='tickets'`); `live` mode bypasses |
| `instances/<name>.json` as files | `instances` + `deployments` tables; the JSON file convention is dropped in favor of rows |
| Files in DB / repo store | R2: `state/` (mutable) + `snapshots/{sha}/` (immutable) + `vault/` (encrypted) |
| VPS control plane | FastAPI in a Cloudflare container (see CLOUD.md) |
| Single operator | Multi-user; `owner_id` on all owned rows; Google auth |

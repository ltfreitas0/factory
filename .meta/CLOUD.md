# Factory in the cloud

Intent, constraints, and non-goals for running factory **off the laptop**.
Shape of the product is [SHAPE.md](SHAPE.md). This file is **where it lives**.

Recorded 2026-08-15. Do not treat this as a ticket.

---

## Intention

Factory is a **project** (this repo, `bixantil/factory`). It is not a product
issued to users. Products (CORPORA and later) live on the **ltfreitas0**
sandbox when they are published. A sanitized public factory clone on
ltfreitas0 waits until factory itself is finished.

The operator should not need the factory on a personal workstation. It
runs in the cloud, **behind a firewall**, with authentication, and keeps
working across factory *code* deploys.

---

## Target topology (Cloudflare-native, ratified 2026-08-16)

```
browser
  → Cloudflare Access (identity) + Pages (SPA, React+Vite)
  → routing Worker → FastAPI container (singleton, getByName)
      ├── D1            persistent remote DB (projects, tickets, runs, tape)
      ├── R2            repo state/ + snapshots/{sha}/ + vault/ (encrypted)
      ├── Sandbox gateway Worker → per-project ephemeral sandbox containers (dev preview)
      ├── opencode/dsh  agent runtime (configurable inference provider)
      └── outbound: GitHub, model provider, user's CF account (deploy via OAuth)
```

- **Pages** — the dashboard SPA only. No implement jobs on Pages.
- **FastAPI container** — the whole control plane: infra manager, sandbox
  controller, agent runtime, state/file mutator. Disk is **ephemeral**; all
  durable state lives in D1 + R2. Containers is beta — the routing Worker is
  the only contact point, so a later platform change stays contained.
- **Sandbox** — dev environments are ephemeral containers under the platform
  account (`getSandbox(ns, normalize(slug))`, sleepAfter 30m, destroy on
  delete). Preview URLs via the gateway Worker (`proxyToSandbox` first,
  custom domain + wildcard DNS required, not `.workers.dev`).
  **Egress:** the base image restricts outbound traffic — package installs
  inside the sandbox (bun/npm) need egress configured (gateway
  `enableInternet`/outbound handler). Verified locally: file sync + serving
  work; `bun install` inside the container fails on tarball download until
  egress is opened.
- **Deploy** — user's own Cloudflare account via OAuth: Workers + Assets
  (`wrangler deploy`, `"assets": {"directory": "./dist"}`) or Pages. The
  platform never deploys to prod implicitly.
- **OAuth** — Google (login), GitHub (repo connect), Cloudflare (deploy
  account). Redirects: Pages origin + `http://localhost:5510/` for local.

---

## Persistence (D1 + R2)

Durable state is **D1** (relational) + **R2** (objects). The container's disk
is ephemeral and holds nothing durable — no volumes, no local SQLite in the
cloud topology. Local dev keeps file-backed SQLite for speed; the D1-backed
`Connection` shim in `factory/db.py` keeps the same sqlite3 surface either way.

| State | Where |
| --- | --- |
| relational (projects, tickets, runs, events, users…) | **D1** — one writer (singleton container) |
| repo code (dev state + snapshots) | **R2** `repo/{owner}/{project}/state/` + `snapshots/{sha}/` |
| secrets | **R2** `vault/{owner}/{project}/` (Fernet-encrypted, never returned as bytes) |
| agent session traces | `feed_log` (D1, capped) + raw session ref on `runs.session_id` |
| logs | `events`/`errors` tables + JSONL mirror |

**Backups:** D1 Time Travel (30-day PITR) covers relational; R2 is already
off-box object durability. No extra backup machinery in v1.

**One writer:** the singleton FastAPI container. Do not attach a second
writer until measured lock pain — the D1 shim is the swap point.

SQL stays behind `factory/db.py`. No ORM “just in case.”

---

## Constraints (do not violate)

1. **One workspace root.** No hardcoded `/home/bix/projects/…` in new
   code. `FACTORY_ROOT` + project slug (and later a git remote).
2. **Auth on the board.** Locally, unset `FACTORY_AUTH_TOKEN` may stay
   open. In the cloud, Access and/or `FACTORY_AUTH_TOKEN` (and later
   factory login) gate `/api/*`. Ingest uses the **project** token only.
3. **SPA API origin.** Production Pages cannot use the Vite proxy.
   `VITE_FACTORY_API` is required; SSE goes to the routing Worker host,
   not through Pages Functions.
4. **Runner runs inside the FastAPI container** (agent runtime is a
   container process, not a thread inside `--reload` uvicorn — in the
   container, `factory-runner` is a sibling supervisor process; locally,
   the API thread is fine).
5. **Prod dispatch is never implicit.** Dev may run from a plugin
   stage. Production is a human dispatch (or Access-protected button).
6. **No auto-processing of the message bucket into prod.** Handlers
   may land tickets; autonomy only skips the *issuance* gate.
7. **Secrets:** vault files + env in the container (envVars / secrets
   store). Do not bake `.meta/sandbox.env` into images. Rotate sandbox
   tokens when this repo is no longer a toy.
8. **ltfreitas0** = product sandbox remotes. **bixantil** = factory
   (this project). Do not invert that.

---

## Compatibility with the laptop

The same binary runs at home: `FACTORY_DB=data/factory.db`,
`FACTORY_ROOT` defaulting to the parent of this checkout’s neighbor
projects, API on 127.0.0.1. Cloud is configuration, not a fork.

---

## Out of scope until factory is “finished”

- Sanitized ltfreitas0/factory public clone
- Multi-region / HA factory
- In-product factory chat agent
- Feature-branch instances by default


---

## Live (2026-08-15)

| Piece | Where |
| --- | --- |
| VPS | Hetzner `factory` cx23 hel1 `65.109.237.105` — SSH only, firewall `factory-base` |
| API | `https://factory-api.henosis.cc` (tunnel `factory` → 127.0.0.1:8051) |
| UI | `https://factory.henosis.cc` (Pages project `factory`, Access) |
| State | `/var/lib/factory` on the VPS |
| Board token | `~/.config/factory/board.token` (not in git) |

Jarvis (`jarvis.henosis.cc`, `jarvis-api.henosis.cc`, VPS `jarvis-cloud`) is a separate stack. Do not reuse its tunnel, Pages project, or firewall.

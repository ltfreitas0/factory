# Factory

Full-stack platform that builds, deploys, and serves webapps on Cloudflare. A human stays in the loop for ideation, refinement, and feedback. Factory plans, implements, validates, deploys, watches production, and turns failures back into work.

## What it does

- **Sandbox preview**: per-project ephemeral containers via Cloudflare Sandbox, dev server hot-reload, preview iframe
- **Production deploy**: build in sandbox → Cloudflare Pages direct upload (no wrangler at runtime)
- **Agent loop**: dsh headless plans and implements; user approves plans and merges
- **Versioning**: R2 snapshots (immutable) + state/ (mutable dev), git optional
- **Deploy format**: Pages + Container backend + D1 (see `.meta/DATA.md`)

## Local dev

```bash
uv sync --extra dev
uv run pytest
set -a && source .env && set +a
uv run factory-api          # 127.0.0.1:8051  (API + runner)
cd web && bun install && bun run dev   # 127.0.0.1:5510  (SPA, proxies /api)
cd sandbox-gw && bun install && npx wrangler dev --port 8787  # sandbox gateway
```

## Ports

| Service | Port | Notes |
|---------|------|-------|
| API | 8051 | FastAPI + background runner |
| Web | 5510 | React SPA (Vite), proxies /api |
| Sandbox gateway | 8787 | Cloudflare Sandbox wrapper |

## Layout

| Path | Role |
|------|------|
| `factory/routers/` | HTTP routes (auth, projects, tickets, platform, files, tape) |
| `factory/api.py` | App assembly, lifespan, middleware |
| `factory/db.py` | D1/shite schema + migrations |
| `factory/files.py` | Repo/vault storage (sqlite + R2) |
| `factory/deploy.py` | Pages direct-upload + legacy pipeline runner |
| `factory/worker.py` | dsh adapter (run + stream think/token/tool) |
| `factory/infra.py` | Branches, snapshots, instances, deployments |
| `factory/sandbox.py` | Gateway client (ensure/sync/preview) |
| `factory/machine.py` | Per-project state machine (kind × status × actor) |
| `sandbox-gw/` | Cloudflare Sandbox gateway Worker |
| `web/src/Workspace.tsx` | Main dashboard: rail + preview + tabbed panel |
| `web/src/Board.tsx` | Legacy kanban view |

## Commands

```bash
uv sync --extra dev
uv run pytest
uv run factory-api          # :8051, starts runner thread
cd web && bun install && bun run dev   # :5510, proxies /api
cd sandbox-gw && npx wrangler dev --port 8787  # local sandbox
```

## Do not

- Commit `.env` (credentials only in env/secrets)
- Resume headless sessions — pass ticket/plan from the table
- Put factory state in the product worktree
- Push real credentials (purge from git history before going public)

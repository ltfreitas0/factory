# Factory

Full-stack platform that builds, deploys, and serves webapps on Cloudflare.

## Layout

| Path | Role |
|------|------|
| `factory/routers/` | HTTP routes (auth, projects, tickets, platform, files, tape) |
| `factory/api.py` | App assembly: lifespan, middleware, router includes |
| `factory/db.py` | D1/shite schema + migrations (supports both local sqlite and D1) |
| `factory/files.py` | Repo/vault storage (sqlite + R2 backend) |
| `factory/deploy.py` | Pages direct-upload + legacy pipeline runner |
| `factory/worker.py` | dsh adapter (run + stream think/token/tool/usage) |
| `factory/infra.py` | Branches, snapshots, instances, deployments |
| `factory/sandbox.py` | Gateway client (ensure/sync/preview) |
| `factory/templates.py` | Scaffold templates (vite-react) |
| `factory/machine.py` | Per-project state machine (kind × status × actor) |
| `sandbox-gw/` | Cloudflare Sandbox gateway Worker (TS, @cloudflare/sandbox) |
| `web/src/Workspace.tsx` | Main dashboard: rail + streaming chat + preview iframe |
| `web/src/Board.tsx` | Legacy kanban view |

## Commands

```bash
uv sync --extra dev
uv run pytest
uv run factory-api          # :8051, starts runner thread
cd web && bun install && bun run dev   # :5510, proxies /api
cd sandbox-gw && npx wrangler dev --port 8787  # local sandbox gateway
```

## Ports

- API `8051`
- Web `5510`
- Sandbox gateway `8787`

## Do not

- Commit `.env` (credentials only in env/secrets)
- Resume headless sessions — pass ticket/plan from the table
- Put factory state in the product worktree
- Push real credentials (purge from git history before going public)

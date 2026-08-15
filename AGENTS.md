# Factory

Local software factory. Tickets live in SQLite. dsh headless does one stage per process. Humans approve plans and merges.

## Layout

| Path | Role |
|------|------|
| `factory/sm.py` | Legal ticket transitions |
| `factory/store.py` | Tickets, documents, runs, events |
| `factory/db.py` | SQLite schema |
| `factory/obs.py` | JSONL logs + `errors` table |
| `factory/runner.py` | Dispatcher loop |
| `factory/worker.py` | `dsh --profile headless` |
| `factory/api.py` | FastAPI + background runner |
| `web/` | React SPA (Vite, bun) |

## Commands

```bash
uv sync --extra dev
uv run pytest
uv run factory-api          # :8051, starts runner thread
cd web && bun install && bun run dev   # :5510, proxies /api
```

## Ports

- API `8051`
- Web `5510`

## Slice

Default product: `corpora` (`/home/bix/projects/corpora`). Playground remains a sandbox project.

inbox → accept → planning (dsh) → plan_review (you) → implementing (dsh) → validating → merge_review (you) → done

UI: kanban + orange cycle + left agent SSE feed (`/api/stream`).

## Do not

- Commit `.env`
- Resume headless sessions — pass ticket/plan from the table
- Put factory state in the product worktree

# factory

Tickets in SQLite. dsh headless plans and implements. You approve plans and merges.

Default product is **corpora** (`/home/bix/projects/corpora`). The board at `:5510` shows that project. Accept the inbox ticket to start a run; the left column is the live agent feed.

Tickets are `build` (plan → implement → validate → merge) or `validate` (accept → run `scripts/validate` → done/failed, no merge). Use validate tickets to simulate scenarios and prove the product still works.

```bash
uv sync --extra dev
uv run pytest
set -a && source .env && set +a
uv run factory-api          # 127.0.0.1:8051
cd web && bun install && bun run dev   # 127.0.0.1:5510
```

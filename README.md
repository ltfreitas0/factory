# factory

Tickets in SQLite. dsh headless plans and implements. You approve plans and merges.

```bash
uv sync --extra dev
uv run pytest
set -a && source .env && set +a
uv run factory-api          # 127.0.0.1:8051
cd web && bun install && bun run dev   # 127.0.0.1:5510
```

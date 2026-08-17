#!/usr/bin/env bash
# Deploy the factory SPA to Cloudflare Pages project `factory`.
# Custom domain: factory.henosis.cc
# Does not touch the `jarvis` Pages project.
set -euo pipefail
cd "$(dirname "$0")/.."

CREDS="${CLOUDFLARE_CREDS:-$HOME/base/sensitive/cloudflare-creds.txt}"
ACCT="8aeff78671e78879236d2fefbebe63b1"
TOKEN="$(python3 - <<'PY'
from pathlib import Path
text = Path.home().joinpath("base/sensitive/cloudflare-creds.txt").read_text().splitlines()
for i,l in enumerate(text):
    if "main-acc-all" in l:
        for j in range(i, min(i+15, len(text))):
            if text[j].startswith("cfat_"):
                print(text[j].strip()); raise SystemExit
raise SystemExit("missing main-acc-all token")
PY
)"
test -n "$TOKEN"

export VITE_FACTORY_API="${VITE_FACTORY_API:-https://factory-api.henosis.cc}"
bun install --cwd web --frozen-lockfile
bun run --cwd web build
export CLOUDFLARE_API_TOKEN="$TOKEN" CLOUDFLARE_ACCOUNT_ID="$ACCT"
~/.bun/bin/wrangler pages deploy web/dist --project-name factory --branch main

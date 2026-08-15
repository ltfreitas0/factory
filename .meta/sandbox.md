# Sandbox credentials

Values live in [`sandbox.env`](sandbox.env). Source before journeys:

```bash
set -a && source .meta/sandbox.env && set +a
```

| Use | Vars |
| --- | --- |
| CF account / Workers / R2 (no custom domain) | `CLOUDFLARE_*`, `R2_*` |
| CF OAuth (connect account in UI) | `CF_OAUTH_*` |
| GitHub API / repos | `GITHUB_TOKEN`, `GITHUB_OWNER` |
| GitHub OAuth (connect repo in UI) | `GITHUB_OAUTH_*` |

Redirect for both OAuth apps is `http://localhost:5510/` (factory Vite).
Journeys: `.meta/SHAPE.md` §6.3.

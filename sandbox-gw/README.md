# sandbox-gw

Cloudflare Sandbox gateway Worker: a thin, authenticated HTTP API over the
[Cloudflare Sandbox SDK](https://developers.cloudflare.com/sandbox/) (stable
`@cloudflare/sandbox` line). The factory FastAPI backend drives one ephemeral
container per project through this Worker; preview URLs are proxied through
the same Worker.

## Layout contract

- One sandbox per project: `getSandbox(env.Sandbox, <project>, { normalizeId: true, sleepAfter: "30m", enableDefaultSession: false })`
- Files live under `/workspace/{project}/` (mirrors `repo/{owner}/{project}/state/`)
- `destroy()` on project delete; auto-sleep after 30m of inactivity
- `normalizeId: true` lowercases the sandbox id — the id returned by
  `/ensure` (`sandbox_id`) is the lowercase project slug
- Sessionless exec (`enableDefaultSession: false`): every command runs in a
  fresh process; if a command kills its own shell (e.g. `exit N`), the gateway
  resets the sandbox and still returns a structured `{ok, stdout, stderr,
  exitCode}` — the container filesystem is ephemeral by contract, clients
  re-sync from R2 `state/`

## API

All routes require `Authorization: Bearer <GATEWAY_TOKEN>`. JSON bodies are
UTF-8. Errors are `{ok: false, error, code?}` with the corresponding HTTP
status (`400` bad request, `401` unauthorized, `404` missing, `5xx`
container/platform errors).

| Route | Body | Returns |
| --- | --- | --- |
| `POST /ensure` | `{project}` | `{ok, sandbox_id}` — creates/gets the sandbox, waits for the container (3 tries, 2s backoff) |
| `POST /exec` | `{project, command, timeout?}` (timeout seconds, default 300, max 3600) | `{ok, stdout, stderr, exitCode}` — `ok` is `exitCode === 0`; runs with `cwd=/workspace/{project}`; commands that kill their own shell are reported as `{ok:false, exitCode}` and the sandbox is reset for the next call |
| `PUT /files/{project}/{path}` | `{content, encoding?}` (`encoding`: `"utf-8"` default, `"base64"` for binary) | `{ok}` — writes under `/workspace/{project}/{path}` (parents created) |
| `GET /files/{project}/{path}` | — | `{ok, content, encoding}` (`"utf-8"` or `"base64"`; binary files are auto-detected and base64-encoded); missing file → 404 |
| `POST /ports/expose` | `{project, port, hostname?}` (port 1024–65535; hostname defaults to the gateway's own host) | `{ok, url}` — preview URL for a service running in the sandbox (port 3000 is reserved by the sandbox control plane) |
| `POST /sync` | `{project}` | `{ok, workspace}` — ensures `/workspace/{project}` exists |
| `POST /sleep` | `{project}` | `{ok, note}` — stable SDK has no imperative sleep; containers auto-sleep via the `sleepAfter` option |
| `POST /destroy` | `{project}` | `{ok}` — tears the sandbox down |

Paths are validated: absolute paths, backslashes, and `..` segments are
rejected (HTTP 400).

### Examples

```bash
TOKEN=... # from `wrangler secret put GATEWAY_TOKEN`

curl -s -X POST localhost:8787/ensure \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"project":"my-project"}'

curl -s -X POST localhost:8787/exec \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"project":"my-project","command":"python3 -c \"print(2+2)\"","timeout":60}'

curl -s -X PUT "localhost:8787/files/my-project/hello.txt" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"content":"hi"}'

curl -s -X POST localhost:8787/ports/expose \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"project":"my-project","port":8080}'
```

## Local development

```bash
bun install        # or: npm install
cp .dev.vars.example .dev.vars   # GATEWAY_TOKEN=dev-secret
npx wrangler dev   # http://localhost:8787; builds the Docker image on first run
```

First `wrangler dev` builds the sandbox container image from `./Dockerfile`
(allow a few minutes). Docker must be running.

## Deploy

```bash
npx wrangler secret put GATEWAY_TOKEN   # prompts for the token
npx wrangler deploy                     # builds the image, pushes the Worker
```

Typecheck before shipping: `npx tsc --noEmit`.

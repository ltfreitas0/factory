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

## Target topology (Hetzner + Cloudflare)

```
browser
  → Cloudflare Access (identity) + Pages (SPA)
  → tunnel hostname (API + SSE)
      → VPS 127.0.0.1:8051   factory-api
      → VPS worker process   factory-runner
      disk: SQLite + git clones + ~/.dsh
      outbound: GitHub, CF API, model provider
```

- **Pages** — static board only. No implement jobs on Workers/Pages.
- **Tunnel** — the only inbound to the VPS. Hetzner firewall: deny public
  8051/5510. SSH optional (your IP) or console-only.
- **VPS** — the factory machine: API, runner, git worktrees, dsh, SQLite.
- **Access** — “not on the open internet” for *you*. Product **ingest
  tokens** stay separate (machines talking in).

OAuth apps (GitHub, CF) get the Pages origin as a redirect, in addition
to `http://localhost:5510/` for local work.

---

## Persistence (SQLite)

A remote SQL database is **not** required for this topology.

| State | Where |
| --- | --- |
| `FACTORY_DB` | **Volume / durable path**, e.g. `/var/lib/factory/factory.db` — never inside the git checkout or an image layer |
| logs | `/var/lib/factory/logs` or `FACTORY_LOG` |
| clones + worktrees | `FACTORY_ROOT` (also on the volume) |
| dsh home | `$HOME/.dsh` on the VPS |

**Deploying factory** (git pull, new image, systemd restart) must not
replace that volume. The process is cattle; the file is the pet.

**Backups:** periodic SQLite backup (`VACUUM INTO` / `.backup`) to **R2**.
Restore = copy onto the volume before the API starts. That is off-box
durability without Postgres.

WAL + **one writer** (one API+runner pair on one box). Do not put the
file on NFS or attach two VMs.

**When a network DB becomes real** (not “we redeployed”):

- two factory instances writing at once
- API on a Worker and runner on a VM
- measured lock/size pain

Until then, keep SQL behind `factory/db.py`. No ORM “just in case.”

---

## Constraints (do not violate)

1. **One workspace root.** No hardcoded `/home/bix/projects/…` in new
   code. `FACTORY_ROOT` + project slug (and later a git remote).
2. **Auth on the board.** Locally, unset `FACTORY_AUTH_TOKEN` may stay
   open. In the cloud, Access and/or `FACTORY_AUTH_TOKEN` (and later
   factory login) gate `/api/*`. Ingest uses the **project** token only.
3. **SPA API origin.** Production Pages cannot use the Vite proxy.
   `VITE_FACTORY_API` (or same-host tunnel) is required. SSE goes to
   the tunnel host, not through Pages Functions.
4. **Runner is a process**, not a thread inside `--reload` uvicorn.
   systemd: `factory-api` + `factory-runner`.
5. **Prod dispatch is never implicit.** Dev may run from a plugin
   stage. Production is a human dispatch (or Access-protected button).
6. **No auto-processing of the message bucket into prod.** Handlers
   may land tickets; autonomy only skips the *issuance* gate.
7. **Secrets:** vault files + env on the VPS. Do not bake
   `.meta/sandbox.env` into images. Rotate sandbox tokens when this
   repo is no longer a toy.
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
- Postgres/D1
- In-product factory chat agent
- Feature-branch instances by default

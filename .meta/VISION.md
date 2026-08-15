# Factory — product vision

Recorded 2026-08-15. This is the destination, not the current build. Near-term UI/obs work stays in [`todo.md`](todo.md). Do not implement this file as one ticket.

**One-liner.** Factory is a full-stack product **builder, maintainer, and manager**. A human stays in the loop for ideation, refinement, and feedback. The factory does the rest: tickets, agents, deploys, errors, integrations.

Reduced shape (board + files + messages): [SHAPE.md](SHAPE.md).
Cloud host + SQLite persistence: [CLOUD.md](CLOUD.md).
This repo is the factory **project** (`bixantil/factory`). Issued products use the ltfreitas0 sandbox.

Today the human (via Grok) *is* the factory agent, issuing batches by hand. That role becomes a **first-class in-product agent**. The operator’s job is mostly **user**, not orchestrator.

---

## Who it is for

You. You invent, steer, approve, and judge. Factory plans, implements, validates, deploys, watches production, and turns failures back into work — with you still holding the gates.

Quality control is **not solved**. Good code still matters. See *Open: quality* below.

---

## Shape

```
You  ←→  Factory agent  ←→  Projects
                 │
                 ├── Tickets (parallel)
                 ├── Deployments (N named instances)
                 ├── Infra + CI/CD plugins
                 ├── Git (GitHub App)
                 ├── Integrations (remote systems)
                 └── Errors (raw bucket → some become tickets)
```

### Projects

Many products. CORPORA is one. Each project has its own repo, tickets, deployments, credentials, and error stream.

### Tickets

- Parallel work, not one-ticket-at-a-time forever.
- Ticket changes land on a **selected deployment** (dev / canary / experiment / …), not blindly on production.
- Factory may **spawn** tickets (`proposed`). A human must approve before they run. You stay in control.
- A **merge** operation promotes a selected deployment onto **production** and updates that deployment. Merge is performed by **CI/CD or an agent** — mechanism TBD.

### Deployments (instances)

A project holds **N served instances**. You define how many and what they are named.

Examples: `production`, `development`, `canary`, `experimental`, or whatever you invent.

- One instance is flagged **production** (built-in flag, not just a name).
- Tickets target a non-prod instance by default (or the one you select).
- Promoting to production is an explicit merge, not an implicit side effect of “done.”

---

## Infra and CI/CD — extensible, any environment

The factory must work in **any** environment. That means **plugins**, not one vendor baked in.

| Layer | Pattern | Notes |
| --- | --- | --- |
| Infra | Per-project / per-plugin provisioners | Cloudflare, Fly, bare metal, local, … |
| CI/CD | Per-project pipeline adapter | GitHub Actions, local runner, agent-run merge, … |
| Secrets | Entered and stored via the **UI** | Passed into the selected environment. Never commit. |

Credentials in the UI imply **new backend models**: projects, environments, secret slots, bindings (which secret goes to which instance), and an audit trail of who set what. Roll/rotate later.

CI/CD details are **unknown**. Design the **seams** first (interfaces + config), not a single pipeline.

---

## Git

Programmatic Git via a **GitHub App + OAuth**.

- Connect a repo to a factory project.
- Show the **code itself** in the factory UI (browse, diff, PR/merge context).
- Ticket worktrees / branches stay the implementation path; GitHub is the remote source of truth.

---

## Factory agent

The factory has **its own agent**.

So far that agent has been an external session (Grok) filing tickets and watching dsh. That becomes **inbuilt**:

- Talk to the factory: ideate, file work, refine plans, ask “why is this slow.”
- It may spawn `proposed` tickets from gaps, todos, and production errors.
- It does **not** auto-accept, auto-approve plans, or auto-merge to production.
- Project workers (dsh / other harnesses) remain the implementers unless you change that.

Human gates stay: accept spawn, approve plan, approve merge, choose deployment, promote to production.

---

## Integration layer

A remote **integration surface** so other systems can talk to Factory (and Factory to them): HTTP + MCP (and later whatever you add).

Use cases: issue a ticket from another app, attach a deploy hook, push errors in, query project state. Auth is first-class. Same idea as CORPORA: machine interface is not an afterthought.

---

## Errors — central, then promote

Factory **centralizes error collection**.

```
deploy / runtime / worker
        ↓
   raw error bucket   (per project, persistent)
        ↓
   some promoted → tickets (proposed)
        ↓
   you approve → factory works them
```

- **Raw bucket:** every deployment/runtime failure lands here. Not every line is a ticket.
- **Promotion:** rules or the factory agent lifts a subset into `proposed` tickets on that project.
- Production failures **pipe into the factory-project** that owns the deploy. The factory’s own failures go to a factory project (dogfood).

Today we have a thin `errors` table + jsonl. The vision is a first-class ingest API, retention, grouping, and promotion — not a log pane only.

---

## You remain the user

| You | Factory |
| --- | --- |
| Ideation, refinement, taste | Plan, implement, validate |
| Approve spawn / plan / merge | Run workers, deploys, watches |
| Pick deployments, name instances | Provision via plugins |
| Paste/rotate credentials in UI | Bind secrets to environments |
| Quality bar (still open) | Tests, review gates, traces |

---

## Open: quality

Want it or not, **good code is important**. How Factory enforces that is undecided. Candidates (not chosen):

- Tests + validate command as the only gate (today).
- Mandatory plan/merge review (today, human).
- A review agent that cannot be the implementer (fresh context).
- GitHub PR + human/CI on the production merge path.
- Showing the code in-product so you can actually look.

Do not pretend a green `scripts/validate` is a quality system. Record failures and reviews as data (see `todo.md` observability) so we can study behavior.

---

## Data models this implies (later)

Sketch only — not a schema ticket.

- `projects` (already) + `environments` / `instances` (name, production flag, url, plugin)
- `deployments` (instance + git sha + status)
- `credentials` / `secret_bindings` (UI-set, encrypted at rest)
- `integrations` (kind, config, auth)
- `error_events` (raw bucket) + `promotions` (error → ticket)
- `git_connections` (GitHub App installation, repo)
- richer `tickets` (target instance, parent, source=human|factory|error)
- agent sessions / traces (already queued in `todo.md`)

---

## What this is not (yet)

Not Archon. Not QM. Not “the factory is CORPORA.” CORPORA is a **product issued to** Factory.

Not auto-prod. Not auto-approve. Not a single cloud.

---

## Suggested horizon (when we get there)

1. Finish near-term: live traces, cost, events column, ticket popover (`todo.md`).
2. Parallel tickets + multi-project as a first-class board.
3. Environments + “ticket → selected deploy”; local/dev first.
4. Secrets in the UI + plugin interface (infra, then CI/CD).
5. GitHub App, code view, merge-to-production as an operation.
6. In-product factory agent.
7. Error ingest + raw bucket + promote-to-ticket.
8. Integration/MCP layer for remote systems.

Each of those is many factory tickets. This file is the north star.

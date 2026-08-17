import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, Plus, RefreshCw } from 'lucide-react'
import Agent from './Agent'
import { Button } from './components/ui/button'
import { Inspector, RepoTree, type InspectorFile } from './Inspector'
import { CollapsedStrip, ColumnHead, useColumnOpen } from './Rail'
import { api, apiBase, authToken, type Project } from './lib/api'
import { cn } from './lib/utils'

type Ticket = {
  id: string
  project_id: string
  project?: string
  state: string
  title: string
  body: string
  kind?: string
  tokens?: number
  usd?: number
  updated_at: string
}

type Doc = { id: string; kind: string; version: number; body: string; author: string }
type Run = { id: string; stage: string; status: string; stdout: string | null; stderr: string | null }
type Detail = Ticket & { documents: Doc[]; runs: Run[] }
type Err = { id: number; source: string; level: string; message: string; at: string; ticket_id: string | null }
type Health = {
  ok: boolean
  open_tickets: number
  errors: number
  active?: { id: string; state: string; title: string } | null
}
type Costs = { tokens: number; usd: number; estimate: boolean }
type FeedItem = {
  at: string
  kind: string
  text: string
  ticket_id?: string | null
  state?: string | null
  title?: string | null
}
type Live = { id: string | null; state: string; title: string }

const CYCLE = [
  { id: 'inbox', label: 'inbox' },
  { id: 'planning', label: 'plan' },
  { id: 'plan_review', label: 'review' },
  { id: 'implementing', label: 'build' },
  { id: 'validating', label: 'check' },
  { id: 'merge_review', label: 'merge' },
  { id: 'done', label: 'done' },
] as const

function cycleId(state: string): string {
  if (state === 'ready_to_plan') return 'planning'
  if (state === 'ready_to_validate') return 'validating'
  if (state === 'pr_open' || state === 'integrating') return 'merge_review'
  if (state === 'failed' || state === 'needs_human') return state
  return state
}

const COLS = [
  'inbox',
  'planning',
  'plan_review',
  'implementing',
  'validating',
  'merge_review',
  'done',
  'failed',
] as const

const ROWS: (typeof COLS)[number][][] = [COLS.slice(0, 4), COLS.slice(4)]

const COL_LABEL: Record<string, string> = {
  inbox: 'Inbox',
  planning: 'Planning',
  plan_review: 'Review',
  implementing: 'Build',
  validating: 'Check',
  merge_review: 'Merge',
  done: 'Done',
  failed: 'Failed',
}

const PENDING_STATES = new Set(['proposed', 'plan_review', 'merge_review'])

function pendingLabel(t: Ticket): string {
  if (t.state === 'proposed') return 'approve spawn'
  if (t.state === 'plan_review') return 'approve plan'
  if (t.state === 'merge_review') return 'approve merge'
  return t.state
}

function latest(docs: Doc[], kind: string): Doc | undefined {
  return docs.filter((d) => d.kind === kind).sort((a, b) => b.version - a.version)[0]
}

function applyFeed(prev: FeedItem[], item: FeedItem): FeedItem[] {
  if (item.kind === 'snapshot') return prev
  if (item.kind === 'token' || item.kind === 'think') {
    const last = prev[prev.length - 1]
    if (last && last.kind === item.kind && last.ticket_id === item.ticket_id) {
      return [...prev.slice(0, -1), { ...last, text: last.text + item.text }].slice(-400)
    }
    return [...prev, item].slice(-400)
  }
  const next = [...prev, item]
  return next.length > 400 ? next.slice(-400) : next
}

function CycleDial({ live }: { live: Live | null }) {
  const cx = 120
  const cy = 120
  const r = 72
  const active = live ? cycleId(live.state) : ''
  const running = Boolean(live && live.state && live.state !== 'done' && live.state !== 'inbox')
  const activeLabel = CYCLE.find((s) => s.id === active)?.label ?? (active || 'idle')

  return (
    <div className="flex h-full min-h-0 flex-col items-center justify-center px-2 py-2">
      <div className="mb-1 text-[11px] tracking-[0.14em] text-faint">CYCLE</div>
      <svg viewBox="0 0 240 240" className="h-[min(220px,100%)] w-[220px] max-w-full">
        <circle cx={cx} cy={cy} r={r} fill="none" stroke="#181818" strokeWidth="1.5" />
        {running && (
          <circle
            className="cycle-spin"
            cx={cx}
            cy={cy}
            r={r}
            fill="none"
            stroke="#ea580c"
            strokeWidth="1.5"
            strokeDasharray="6 18"
            strokeLinecap="round"
            opacity="0.7"
          />
        )}
        {CYCLE.map((step, i) => {
          const angle = (i / CYCLE.length) * Math.PI * 2 - Math.PI / 2
          const x = cx + r * Math.cos(angle)
          const y = cy + r * Math.sin(angle)
          const lx = cx + (r + 24) * Math.cos(angle)
          const ly = cy + (r + 24) * Math.sin(angle)
          const on = active === step.id
          return (
            <g key={step.id}>
              <circle
                cx={x}
                cy={y}
                r={on ? 6 : 4}
                className={on ? 'cycle-live' : undefined}
                fill={on ? '#ea580c' : '#0c0c0c'}
                stroke={on ? '#ea580c' : '#3f3f46'}
                strokeWidth="1.5"
              />
              <text
                x={lx}
                y={ly}
                textAnchor="middle"
                dominantBaseline="middle"
                fill={on ? '#ea580c' : '#6b6b76'}
                fontSize="11"
              >
                {step.label}
              </text>
            </g>
          )
        })}
        <text x={cx} y={cy - 6} textAnchor="middle" fill="#ea580c" fontSize="13">
          {activeLabel}
        </text>
        <text x={cx} y={cy + 12} textAnchor="middle" fill="#6b6b76" fontSize="10">
          {live?.title ? live.title.slice(0, 18) : 'idle'}
        </text>
      </svg>
    </div>
  )
}

export default function Board({ slug, onBack }: { slug: string; onBack: () => void }) {
  const [project, setProject] = useState<Project | null>(null)
  const [tickets, setTickets] = useState<Ticket[]>([])
  const [sel, setSel] = useState<string | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [errors, setErrors] = useState<Err[]>([])
  const [health, setHealth] = useState<Health | null>(null)
  const [costs, setCosts] = useState<Costs | null>(null)
  const [title, setTitle] = useState('')
  const [adding, setAdding] = useState(false)
  const [steer, setSteer] = useState('')
  const [busy, setBusy] = useState(false)
  const [flash, setFlash] = useState<string | null>(null)
  const [feed, setFeed] = useState<FeedItem[]>([])
  const [live, setLive] = useState<Live | null>(null)
  const [rail, setRail] = useState<'pending' | 'tree'>('pending')
  const [tab, setTab] = useState(() => {
    const saved = sessionStorage.getItem('factory-tab')
    if (saved) sessionStorage.removeItem('factory-tab')
    return saved || 'apps'
  })
  const [file, setFile] = useState<InspectorFile | null>(null)
  const [treeTick, setTreeTick] = useState(0)
  const [agentOpen, toggleAgent] = useColumnOpen('agent')
  const [inspectOpen, toggleInspect] = useColumnOpen('inspector')
  const [feedOpen, toggleFeed] = useColumnOpen('feed')
  const feedEnd = useRef<HTMLDivElement>(null)
  const selRef = useRef<string | null>(null)
  selRef.current = sel

  const load = useCallback(async () => {
    const [ts, es, h, c, p] = await Promise.all([
      api<Ticket[]>(`/api/tickets?project=${encodeURIComponent(slug)}`),
      api<Err[]>('/api/errors?limit=50'),
      api<Health>('/api/health'),
      api<Costs>(`/api/costs?project=${encodeURIComponent(slug)}`).catch(() => null),
      api<Project>(`/api/projects/${encodeURIComponent(slug)}`),
    ])
    setTickets(ts)
    setErrors(es)
    setHealth(h)
    if (c) setCosts(c)
    setProject(p)
    if (selRef.current) setDetail(await api<Detail>(`/api/tickets/${selRef.current}`))
  }, [slug])

  useEffect(() => {
    load().catch((e) => setFlash(String(e)))
  }, [load])

  useEffect(() => {
    const tok = authToken()
    const url = tok
      ? `${apiBase()}/api/stream?token=${encodeURIComponent(tok)}`
      : `${apiBase()}/api/stream`
    const es = new EventSource(url)
    es.onmessage = (ev) => {
      try {
        const item = JSON.parse(ev.data) as FeedItem
        if (item.kind === 'snapshot' || item.kind === 'cycle') {
          setLive(
            item.state
              ? { id: item.ticket_id ?? null, state: item.state, title: item.title || item.text }
              : null,
          )
        }
        if (item.kind === 'cycle') load().catch(() => {})
        setFeed((prev) => applyFeed(prev, item))
      } catch {
        /* ignore */
      }
    }
    return () => es.close()
  }, [load])

  useEffect(() => {
    feedEnd.current?.scrollIntoView({ block: 'end' })
  }, [feed])

  const grouped = useMemo(() => {
    const m: Record<string, Ticket[]> = {}
    for (const c of COLS) m[c] = []
    for (const t of tickets) {
      if (t.state === 'proposed') continue
      const mapped =
        t.state === 'ready_to_plan'
          ? 'planning'
          : t.state === 'ready_to_validate'
            ? 'validating'
            : t.state === 'pr_open' || t.state === 'integrating'
              ? 'merge_review'
              : t.state === 'needs_human'
                ? 'failed'
                : t.state
      const col = COLS.includes(mapped as (typeof COLS)[number]) ? mapped : 'failed'
      ;(m[col] ??= []).push(t)
    }
    return m
  }, [tickets])

  async function act(fn: () => Promise<unknown>) {
    setBusy(true)
    setFlash(null)
    try {
      await fn()
      await load()
    } catch (e) {
      setFlash(String(e))
    } finally {
      setBusy(false)
    }
  }

  const plan = detail ? latest(detail.documents, 'plan') : undefined
  const result = detail ? latest(detail.documents, 'result') : undefined
  const streaming = feed.some((f) => f.kind === 'token' || f.kind === 'think')
  const events = feed.filter((f) => f.kind !== 'token' && f.kind !== 'think')
  const stream = feed.filter((f) => f.kind === 'token' || f.kind === 'think' || f.kind === 'tool' || f.kind === 'tool_result' || f.kind === 'usage')
  const pending = tickets.filter((t) => PENDING_STATES.has(t.state))

  function openTicket(id: string) {
    setSel(id)
    api<Detail>(`/api/tickets/${id}`)
      .then(setDetail)
      .catch((e) => setFlash(String(e)))
  }

  function closeTicket() {
    setSel(null)
    setDetail(null)
  }

  return (
    <div className="flex h-full flex-col bg-bg text-fg">
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-border bg-panel px-4">
        <div className="flex min-w-0 items-center gap-3">
          <Button variant="ghost" size="sm" onClick={onBack} title="all projects">
            <ArrowLeft size={16} />
          </Button>
          <span className="text-[16px] tracking-[0.16em] text-accent">FACTORY</span>
          <span className="truncate text-muted">{project?.name || slug}</span>
          <span className="text-[14px] text-faint">
            {health ? `${health.open_tickets} open · ${health.errors} err` : '…'}
          </span>
          {costs && (
            <span className="text-[14px] text-accent">
              {costs.estimate ? 'est. ' : ''}${costs.usd.toFixed(3)}
            </span>
          )}
        </div>
        <Button variant="ghost" size="sm" onClick={() => load()}>
          <RefreshCw size={16} />
        </Button>
      </header>

      {flash && (
        <div className="border-b border-red-900 bg-red-950/40 px-4 py-1.5 text-[14px] text-red-300">
          {flash}
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-[18vw] min-w-[220px] max-w-[280px] shrink-0 flex-col border-r border-border bg-panel">
          <div className="flex min-h-0 flex-[3] flex-col border-b border-border">
            <div className="flex shrink-0 border-b border-border">
              <button
                type="button"
                className={cn('rail-tab flex-1', rail === 'pending' && 'is-on')}
                onClick={() => setRail('pending')}
              >
                pending {pending.length}
              </button>
              <button
                type="button"
                className={cn('rail-tab flex-1', rail === 'tree' && 'is-on')}
                onClick={() => setRail('tree')}
              >
                tree
              </button>
            </div>
            {rail === 'pending' ? (
              <div className="min-h-0 flex-1 overflow-y-auto p-2">
                {pending.length === 0 && (
                  <p className="px-2 py-3 text-[13px] text-faint">nothing waiting on you</p>
                )}
                {pending.map((t) => (
                  <button
                    key={t.id}
                    onClick={() => openTicket(t.id)}
                    className="mb-1 w-full px-2 py-2 text-left hover:bg-panel2"
                  >
                    <div className="text-[11px] tracking-[0.08em] text-accent">{pendingLabel(t)}</div>
                    <div className="line-clamp-2 text-[15px]">{t.title}</div>
                  </button>
                ))}
              </div>
            ) : (
              <RepoTree
                slug={slug}
                selected={file?.path ?? null}
                tick={treeTick}
                onOpen={(path, ref) => {
                  setFile({ path, ref })
                  setTab('file')
                  if (!inspectOpen) toggleInspect()
                }}
              />
            )}
          </div>
          <div className="min-h-0 flex-[2]">
            <CycleDial live={live} />
          </div>
        </aside>

        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          {ROWS.map((row, ri) => (
            <div
              key={ri}
              className={cn('flex min-h-0 min-w-0 flex-1', ri === 0 && 'border-b border-border')}
            >
              {row.map((col) => (
                <section key={col} className="flex min-w-0 flex-1 flex-col border-r border-border">
                  <div className="flex items-center justify-between border-b border-border px-3 py-2 text-[13px] tracking-[0.08em] text-muted">
                    <span>
                      {COL_LABEL[col]} <span className="text-faint">{grouped[col]?.length ?? 0}</span>
                    </span>
                    {col === 'inbox' && (
                      <button
                        type="button"
                        className="text-accent hover:text-fg"
                        title="new ticket"
                        onClick={() => setAdding((v) => !v)}
                      >
                        <Plus size={16} />
                      </button>
                    )}
                  </div>
                  {col === 'inbox' && adding && (
                    <form
                      className="border-b border-border p-2"
                      onSubmit={(e) => {
                        e.preventDefault()
                        if (!title.trim()) return
                        act(async () => {
                          const t = await api<Ticket>('/api/tickets', {
                            method: 'POST',
                            body: JSON.stringify({ title, project: slug, kind: 'build' }),
                          })
                          setTitle('')
                          setAdding(false)
                          openTicket(t.id)
                        })
                      }}
                    >
                      <input
                        autoFocus
                        className="h-9 w-full border-0 border-b border-border bg-transparent text-[14px] outline-none focus:border-accent"
                        placeholder="ticket title"
                        value={title}
                        onChange={(e) => setTitle(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Escape') setAdding(false)
                        }}
                      />
                    </form>
                  )}
                  <div className="flex flex-1 flex-col gap-1 overflow-y-auto p-1.5">
                    {(grouped[col] ?? []).map((t) => (
                      <button
                        key={t.id}
                        onClick={() => openTicket(t.id)}
                        className={cn(
                          'px-2 py-2 text-left text-[15px]',
                          sel === t.id
                            ? 'border-l border-accent bg-accent/10 text-accent'
                            : 'border-l border-transparent hover:bg-panel',
                        )}
                      >
                        <div className="line-clamp-2">{t.title}</div>
                        {(t.usd || 0) > 0 && (
                          <div className="mt-1 text-[12px] text-accent">${Number(t.usd).toFixed(3)}</div>
                        )}
                      </button>
                    ))}
                  </div>
                </section>
              ))}
            </div>
          ))}
        </main>

        <Agent
          projectName={project?.name || slug}
          open={agentOpen}
          onToggle={toggleAgent}
        />

        <Inspector
          tab={tab}
          onTab={setTab}
          open={inspectOpen}
          onToggle={toggleInspect}
          ctx={{
            slug,
            project,
            file,
            onFile: (f) => {
              setFile(f)
              if (f) {
                setTab('file')
                if (!inspectOpen) toggleInspect()
              }
            },
            onAssetsChanged: () => {
              setTreeTick((n) => n + 1)
              load().catch(() => {})
            },
            errors,
          }}
        />

        {feedOpen ? (
          <aside className="flex w-[20vw] min-w-[220px] max-w-[300px] shrink-0 flex-col border-l border-border bg-panel">
            <ColumnHead
              title="live"
              open={feedOpen}
              onToggle={toggleFeed}
              extra={streaming ? <span className="pr-2 text-[11px] text-accent">live</span> : null}
            />
            <div className="flex min-h-0 flex-1 flex-col border-b border-border">
              <div className="px-3 py-1.5 text-[11px] tracking-[0.14em] text-faint">STREAM</div>
              <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3 font-mono text-[13px] leading-5">
                {stream.length === 0 && <p className="text-faint">idle</p>}
                {stream.map((line, i) => (
                  <div
                    key={`s-${line.at}-${i}`}
                    className={cn(
                      'mb-1.5 whitespace-pre-wrap break-words',
                      line.kind === 'think'
                        ? 'text-muted italic'
                        : line.kind === 'tool'
                          ? 'text-accent'
                          : line.kind === 'tool_result'
                            ? 'text-muted'
                            : line.kind === 'usage'
                              ? 'text-faint'
                              : 'text-fg',
                    )}
                  >
                    {line.text}
                    {(line.kind === 'token' || line.kind === 'think') && i === stream.length - 1 && (
                      <span className="ml-0.5 inline-block h-[1em] w-[0.5ch] bg-accent align-text-bottom" />
                    )}
                  </div>
                ))}
                <div ref={feedEnd} />
              </div>
            </div>
            <div className="flex min-h-0 flex-1 flex-col">
              <div className="px-3 py-1.5 text-[11px] tracking-[0.14em] text-faint">EVENTS</div>
              <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3 font-mono text-[12px] leading-5">
                {events.length === 0 && <p className="text-faint">no events</p>}
                {events.map((line, i) => (
                  <div
                    key={`e-${line.at}-${i}`}
                    className={cn(
                      'mb-1.5 whitespace-pre-wrap break-words',
                      line.kind === 'stderr'
                        ? 'text-red-400'
                        : line.kind === 'cycle'
                          ? 'text-accent'
                          : 'text-fg',
                    )}
                  >
                    <span className="text-faint">{line.at} </span>
                    {line.text}
                  </div>
                ))}
              </div>
            </div>
          </aside>
        ) : (
          <CollapsedStrip
            title="live"
            onToggle={toggleFeed}
            extra={streaming ? <span className="text-[10px] text-accent">●</span> : null}
          />
        )}
      </div>

      {detail && (
        <div
          className="fixed inset-0 z-20 flex items-start justify-center bg-black/70 p-8"
          onClick={closeTicket}
        >
          <div
            className="max-h-[90vh] w-full max-w-3xl overflow-y-auto border border-border bg-panel p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-start justify-between gap-3">
              <div>
                <div className="text-[14px] tracking-[0.08em] text-muted">{detail.state}</div>
                <h2 className="text-[24px] leading-snug">{detail.title}</h2>
              </div>
              <Button variant="ghost" size="sm" onClick={closeTicket}>
                close
              </Button>
            </div>
            <p className="whitespace-pre-wrap text-[18px] text-muted">{detail.body}</p>
            <div className="mt-3 flex flex-wrap gap-1">
              {(detail.state === 'inbox' || detail.state === 'proposed') && (
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    act(async () => {
                      await api(`/api/tickets/${detail.id}/accept`, { method: 'POST' })
                      closeTicket()
                    })
                  }
                >
                  {detail.state === 'proposed'
                    ? 'approve spawn'
                    : detail.kind === 'validate'
                      ? 'run validation'
                      : 'accept'}
                </Button>
              )}
              {detail.state === 'plan_review' && (
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    act(async () => {
                      await api(`/api/tickets/${detail.id}/approve-plan`, { method: 'POST' })
                      closeTicket()
                    })
                  }
                >
                  approve plan
                </Button>
              )}
              {detail.state === 'failed' && (
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    act(async () => {
                      await api(`/api/tickets/${detail.id}/action`, {
                        method: 'POST',
                        body: JSON.stringify({ name: 'retry', actor: 'human' }),
                      })
                    })
                  }
                >
                  retry
                </Button>
              )}
              {detail.state === 'merge_review' && (
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    act(async () => {
                      await api(`/api/tickets/${detail.id}/approve-merge`, { method: 'POST' })
                      closeTicket()
                    })
                  }
                >
                  approve merge
                </Button>
              )}
            </div>
            {plan && (
              <section className="mt-4">
                <div className="mb-1 text-[15px] tracking-[0.08em] text-muted">PLAN v{plan.version}</div>
                <pre className="whitespace-pre-wrap border border-border bg-bg p-3 text-[17px] text-fg">
                  {plan.body}
                </pre>
                <textarea
                  className="mt-2 h-24 w-full border border-border bg-bg p-3 text-[18px] outline-none focus:border-accent"
                  placeholder="steer / refine plan"
                  value={steer}
                  onChange={(e) => setSteer(e.target.value)}
                />
                <Button
                  className="mt-1"
                  size="sm"
                  variant="ghost"
                  disabled={busy || !steer.trim()}
                  onClick={() =>
                    act(async () => {
                      await api(`/api/tickets/${detail.id}/documents`, {
                        method: 'POST',
                        body: JSON.stringify({ kind: 'steer', body: steer }),
                      })
                      setSteer('')
                    })
                  }
                >
                  save steer
                </Button>
              </section>
            )}
            {result && (
              <section className="mt-4">
                <div className="mb-1 text-[15px] tracking-[0.08em] text-muted">RESULT</div>
                <pre className="whitespace-pre-wrap border border-border bg-bg p-3 text-[16px]">
                  {result.body}
                </pre>
              </section>
            )}
            {detail.runs[0] && (
              <section className="mt-3 text-faint">
                last run {detail.runs[0].stage} · {detail.runs[0].status}
              </section>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

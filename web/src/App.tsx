import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Activity, Plus, RefreshCw } from 'lucide-react'
import { Button } from './components/ui/button'
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
  default_project?: string
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

const AUTH_KEY = 'factory-token'

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const token = localStorage.getItem(AUTH_KEY)
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((init?.headers as Record<string, string>) || {}),
  }
  if (token) headers.Authorization = `Bearer ${token}`
  const res = await fetch(path, { ...init, headers })
  if (!res.ok) {
    const t = await res.text()
    throw new Error(t || res.statusText)
  }
  return res.json() as Promise<T>
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
  const cx = 140
  const cy = 140
  const r = 92
  const active = live ? cycleId(live.state) : ''
  const running = Boolean(live && live.state && live.state !== 'done' && live.state !== 'inbox')
  const activeLabel = CYCLE.find((s) => s.id === active)?.label ?? (active || 'idle')

  return (
    <div className="flex flex-col items-center gap-2 border-t border-border px-3 py-4">
      <div className="text-[13px] tracking-[0.08em] text-muted">CYCLE</div>
      <svg viewBox="0 0 280 280" className="h-[260px] w-[260px]">
        <circle cx={cx} cy={cy} r={r} fill="none" stroke="#181818" strokeWidth="2" />
        {running && (
          <circle
            className="cycle-spin"
            cx={cx}
            cy={cy}
            r={r}
            fill="none"
            stroke="#ea580c"
            strokeWidth="2"
            strokeDasharray="8 22"
            strokeLinecap="round"
            opacity="0.7"
          />
        )}
        {CYCLE.map((step, i) => {
          const angle = (i / CYCLE.length) * Math.PI * 2 - Math.PI / 2
          const x = cx + r * Math.cos(angle)
          const y = cy + r * Math.sin(angle)
          const lx = cx + (r + 28) * Math.cos(angle)
          const ly = cy + (r + 28) * Math.sin(angle)
          const on = active === step.id
          return (
            <g key={step.id}>
              <circle
                cx={x}
                cy={y}
                r={on ? 9 : 6}
                className={on ? 'cycle-live' : undefined}
                fill={on ? '#ea580c' : '#0c0c0c'}
                stroke={on ? '#ea580c' : '#3f3f46'}
                strokeWidth="2"
              />
              <text
                x={lx}
                y={ly}
                textAnchor="middle"
                dominantBaseline="middle"
                fill={on ? '#ea580c' : '#6b6b76'}
                fontSize="13"
              >
                {step.label}
              </text>
            </g>
          )
        })}
        <text x={cx} y={cy - 8} textAnchor="middle" fill="#ea580c" fontSize="16">
          {activeLabel}
        </text>
        <text x={cx} y={cy + 14} textAnchor="middle" fill="#6b6b76" fontSize="11">
          {live?.title ? live.title.slice(0, 22) : 'idle'}
        </text>
      </svg>
    </div>
  )
}

export default function App() {
  const [tickets, setTickets] = useState<Ticket[]>([])
  const [sel, setSel] = useState<string | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [errors, setErrors] = useState<Err[]>([])
  const [health, setHealth] = useState<Health | null>(null)
  const [costs, setCosts] = useState<Costs | null>(null)
  const [title, setTitle] = useState('')
  const [adding, setAdding] = useState(false)
  const [steer, setSteer] = useState('')
  const [errPane, setErrPane] = useState(false)
  const [settings, setSettings] = useState(false)
  const [ingestOnce, setIngestOnce] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [flash, setFlash] = useState<string | null>(null)
  const [feed, setFeed] = useState<FeedItem[]>([])
  const [live, setLive] = useState<Live | null>(null)
  const feedEnd = useRef<HTMLDivElement>(null)
  const selRef = useRef<string | null>(null)
  selRef.current = sel

  const load = useCallback(async () => {
    const [ts, es, h, c] = await Promise.all([
      api<Ticket[]>('/api/tickets?project=corpora'),
      api<Err[]>('/api/errors?limit=50'),
      api<Health>('/api/health'),
      api<Costs>('/api/costs?project=corpora').catch(() => null),
    ])
    setTickets(ts)
    setErrors(es)
    setHealth(h)
    if (c) setCosts(c)
    if (selRef.current) setDetail(await api<Detail>(`/api/tickets/${selRef.current}`))
  }, [])

  useEffect(() => {
    load().catch((e) => setFlash(String(e)))
  }, [load])

  useEffect(() => {
    const tok = localStorage.getItem(AUTH_KEY)
    const es = new EventSource(tok ? `/api/stream?token=${encodeURIComponent(tok)}` : '/api/stream')
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
        if (item.kind === 'cycle') {
          load().catch(() => {})
        }
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
  const stream = feed.filter((f) => f.kind === 'token' || f.kind === 'think' || f.kind === 'tool')
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
      <header className="flex h-16 items-center justify-between border-b border-border bg-panel px-5">
        <div className="flex items-center gap-4">
          <span className="text-[20px] tracking-[0.16em] text-accent">FACTORY</span>
          <span className="text-muted">CORPORA</span>
          <span className="text-faint">
            {health ? `${health.open_tickets} open · ${health.errors} errors` : '…'}
          </span>
          {costs && (
            <span className="text-accent" title="estimated from dsh session traces">
              {costs.estimate ? 'est. ' : ''}${costs.usd.toFixed(3)} ·{' '}
              {(costs.tokens / 1000).toFixed(1)}k tok
            </span>
          )}
          {live?.title && <span className="text-accent">{live.title}</span>}
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={() => load()}>
            <RefreshCw size={18} />
          </Button>
          <Button variant={settings ? 'default' : 'ghost'} size="sm" onClick={() => setSettings((v) => !v)}>
            settings
          </Button>
          <Button
            variant={errPane ? 'default' : 'ghost'}
            size="sm"
            onClick={() => setErrPane((v) => !v)}
          >
            <Activity size={18} /> health
          </Button>
        </div>
      </header>

      {flash && (
        <div className="border-b border-red-900 bg-red-950/40 px-4 py-2 text-red-300">{flash}</div>
      )}

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-[340px] shrink-0 flex-col overflow-hidden border-r border-border bg-panel">
          <div className="shrink-0 border-b border-border">
            <CycleDial live={live} />
          </div>
          <div className="flex min-h-0 flex-1 flex-col border-b border-border">
            <div className="border-b border-border px-4 py-2.5 text-[14px] tracking-[0.08em] text-muted">
              PENDING <span className="text-faint">{pending.length}</span>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-2">
              {pending.length === 0 && <p className="px-2 py-3 text-faint">nothing waiting on you</p>}
              {pending.map((t) => (
                <button
                  key={t.id}
                  onClick={() => openTicket(t.id)}
                  className="mb-1.5 w-full border border-border bg-bg px-3 py-2.5 text-left hover:border-accent"
                >
                  <div className="text-[13px] tracking-[0.06em] text-accent">{pendingLabel(t)}</div>
                  <div className="line-clamp-2 text-[16px]">{t.title}</div>
                </button>
              ))}
            </div>
          </div>
          <div className="shrink-0 px-4 py-3 text-[13px] text-faint">
            {live?.title ? live.title : 'idle'}
          </div>
        </aside>

        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          {ROWS.map((row, ri) => (
            <div
              key={ri}
              className={cn(
                'flex min-h-0 min-w-0 flex-1',
                ri === 0 && 'border-b border-border',
              )}
            >
              {row.map((col) => (
                <section
                  key={col}
                  className="flex min-w-0 flex-1 flex-col border-r border-border"
                >
                  <div className="flex items-center justify-between border-b border-border px-4 py-3 text-[16px] tracking-[0.08em] text-muted">
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
                        <Plus size={18} />
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
                            body: JSON.stringify({ title, project: 'corpora', kind: 'build' }),
                          })
                          setTitle('')
                          setAdding(false)
                          openTicket(t.id)
                        })
                      }}
                    >
                      <input
                        autoFocus
                        className="h-10 w-full border border-border bg-bg px-2 text-[15px] outline-none focus:border-accent"
                        placeholder="ticket title"
                        value={title}
                        onChange={(e) => setTitle(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Escape') setAdding(false)
                        }}
                      />
                    </form>
                  )}
                  <div className="flex flex-1 flex-col gap-2 overflow-y-auto p-2">
                    {(grouped[col] ?? []).map((t) => (
                      <button
                        key={t.id}
                        onClick={() => openTicket(t.id)}
                        className={cn(
                          'relative px-3 py-3 text-left text-[18px]',
                          sel === t.id
                            ? 'border-l-2 border-accent bg-accent/15 text-accent'
                            : 'border-l-2 border-transparent bg-panel hover:bg-panel2',
                        )}
                      >
                        <div className="line-clamp-2">{t.title}</div>
                        <div className="mt-1.5 font-mono text-[14px] text-faint">
                          {t.kind === 'validate' ? 'validate · ' : ''}
                          {t.id}
                        </div>
                        {(t.usd || 0) > 0 && (
                          <div className="mt-2 text-left text-[13px] text-accent">
                            ${Number(t.usd).toFixed(3)}
                          </div>
                        )}
                      </button>
                    ))}
                  </div>
                </section>
              ))}
            </div>
          ))}
        </main>

        <aside className="flex w-[300px] shrink-0 flex-col border-l border-border bg-panel">
          <div className="border-b border-border px-4 py-3 text-[16px] tracking-[0.08em] text-muted">
            EVENTS
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3 font-mono text-[14px] leading-6">
            {events.length === 0 && <p className="text-faint">no events yet</p>}
            {events.map((line, i) => (
              <div
                key={`e-${line.at}-${i}`}
                className={cn(
                  'mb-1.5 whitespace-pre-wrap break-words',
                  line.kind === 'stderr' ? 'text-red-400' : line.kind === 'cycle' ? 'text-accent' : 'text-fg',
                )}
              >
                <span className="text-faint">{line.at} </span>
                {line.text}
              </div>
            ))}
          </div>
        </aside>

        <aside className="flex w-[380px] shrink-0 flex-col border-l border-border bg-panel">
          <div className="flex items-center justify-between border-b border-border px-4 py-3 text-[16px] tracking-[0.08em] text-muted">
            <span>STREAM</span>
            {streaming && <span className="text-accent">live</span>}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3 font-mono text-[15px] leading-6">
            {stream.length === 0 && (
              <p className="text-faint">thinking + tokens appear here while dsh is running</p>
            )}
            {stream.map((line, i) => (
              <div
                key={`s-${line.at}-${i}`}
                className={cn(
                  'mb-2 whitespace-pre-wrap break-words',
                  line.kind === 'think'
                    ? 'text-muted italic'
                    : line.kind === 'tool'
                      ? 'text-accent'
                      : 'text-fg',
                )}
              >
                {line.kind === 'think' && <span className="mr-2 not-italic text-faint">think</span>}
                {line.kind === 'tool' && <span className="mr-2 text-accent">tool</span>}
                {line.text}
                {(line.kind === 'token' || line.kind === 'think') && i === stream.length - 1 && (
                  <span className="ml-0.5 inline-block h-[1em] w-[0.5ch] bg-accent align-text-bottom" />
                )}
              </div>
            ))}
            <div ref={feedEnd} />
          </div>
        </aside>

        {settings && (
          <aside className="flex w-[340px] shrink-0 flex-col border-l border-border bg-panel">
            <div className="border-b border-border px-4 py-3 text-[15px] tracking-[0.08em] text-muted">
              PROJECT
            </div>
            <div className="flex flex-col gap-3 overflow-y-auto p-4 text-[15px]">
              <p className="text-faint">
                Ingest is one token per project. Rotate invalidates the old value. Pipeline lives
                in files/pipeline.yml.
              </p>
              <Button
                size="sm"
                disabled={busy}
                onClick={() =>
                  act(async () => {
                    const r = await api<{ token: string }>('/api/projects/corpora/ingest-token', {
                      method: 'POST',
                    })
                    setIngestOnce(r.token)
                  })
                }
              >
                rotate ingest token
              </Button>
              {ingestOnce && (
                <pre className="whitespace-pre-wrap break-all border border-border bg-bg p-2 text-[13px]">
                  {ingestOnce}
                  {'\n'}copy now — not shown again
                </pre>
              )}
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() =>
                  act(async () => {
                    await api('/api/projects/corpora/dispatch', {
                      method: 'POST',
                      body: JSON.stringify({ instance: 'dev' }),
                    })
                  })
                }
              >
                dispatch dev
              </Button>
            </div>
          </aside>
        )}

        {errPane && (
          <aside className="flex w-[340px] shrink-0 flex-col border-l border-border bg-panel">
            <div className="border-b border-border px-4 py-3 text-[15px] tracking-[0.08em] text-muted">
              ERRORS
            </div>
            <div className="flex-1 overflow-y-auto">
              {errors.length === 0 && <p className="p-4 text-faint">clean</p>}
              {errors.map((e) => (
                <div key={e.id} className="border-b border-border px-4 py-3">
                  <div className="text-[15px] text-accent">
                    {e.source} · {e.level}
                  </div>
                  <div>{e.message}</div>
                  <div className="text-[14px] text-faint">{e.at}</div>
                </div>
              ))}
            </div>
          </aside>
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
                <div className="mb-1 text-[15px] tracking-[0.08em] text-muted">
                  PLAN v{plan.version}
                </div>
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

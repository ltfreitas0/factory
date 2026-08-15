import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Activity, Plus, RefreshCw } from 'lucide-react'
import { Button } from './components/ui/button'
import { cn } from './lib/utils'

type Ticket = {
  id: string
  project_id: string
  state: string
  title: string
  body: string
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
type FeedItem = { at: string; kind: string; text: string; ticket_id?: string | null; state?: string | null }

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

const COL_LABEL: Record<string, string> = {
  inbox: 'Inbox',
  planning: 'Planning',
  plan_review: 'Plan',
  implementing: 'Build',
  validating: 'Check',
  merge_review: 'Merge',
  done: 'Done',
  failed: 'Failed',
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  })
  if (!res.ok) {
    const t = await res.text()
    throw new Error(t || res.statusText)
  }
  return res.json() as Promise<T>
}

function latest(docs: Doc[], kind: string): Doc | undefined {
  return docs.filter((d) => d.kind === kind).sort((a, b) => b.version - a.version)[0]
}

export default function App() {
  const [tickets, setTickets] = useState<Ticket[]>([])
  const [sel, setSel] = useState<string | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [errors, setErrors] = useState<Err[]>([])
  const [health, setHealth] = useState<Health | null>(null)
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [steer, setSteer] = useState('')
  const [errPane, setErrPane] = useState(false)
  const [busy, setBusy] = useState(false)
  const [flash, setFlash] = useState<string | null>(null)
  const [feed, setFeed] = useState<FeedItem[]>([])
  const feedEnd = useRef<HTMLDivElement>(null)

  const load = useCallback(async () => {
    const [ts, es, h] = await Promise.all([
      api<Ticket[]>('/api/tickets'),
      api<Err[]>('/api/errors?limit=50'),
      api<Health>('/api/health'),
    ])
    setTickets(ts)
    setErrors(es)
    setHealth(h)
    if (sel) setDetail(await api<Detail>(`/api/tickets/${sel}`))
  }, [sel])

  useEffect(() => {
    load().catch((e) => setFlash(String(e)))
    const id = setInterval(() => load().catch(() => {}), 2500)
    return () => clearInterval(id)
  }, [load])

  useEffect(() => {
    const es = new EventSource('/api/stream')
    es.onmessage = (ev) => {
      try {
        const item = JSON.parse(ev.data) as FeedItem
        setFeed((prev) => {
          const next = [...prev, item]
          return next.length > 400 ? next.slice(-400) : next
        })
      } catch {
        /* ignore */
      }
    }
    return () => es.close()
  }, [])

  useEffect(() => {
    feedEnd.current?.scrollIntoView({ block: 'end' })
  }, [feed])

  const grouped = useMemo(() => {
    const m: Record<string, Ticket[]> = {}
    for (const c of COLS) m[c] = []
    for (const t of tickets) {
      const mapped = t.state === 'ready_to_plan' ? 'planning' : t.state === 'pr_open' || t.state === 'integrating' ? 'merge_review' : t.state
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
  const live = health?.active
  const liveState = live ? cycleId(live.state) : ''

  return (
    <div className="flex h-full flex-col bg-bg text-fg">
      <header className="flex flex-col border-b border-border bg-panel">
        <div className="flex h-12 items-center justify-between px-4">
          <div className="flex items-center gap-4">
            <span className="text-[15px] tracking-[0.16em] text-accent">FACTORY</span>
            <span className="text-muted">
              {health ? `${health.open_tickets} open · ${health.errors} errors` : '…'}
            </span>
            {live && <span className="text-faint">· {live.title}</span>}
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => load()}>
              <RefreshCw size={14} />
            </Button>
            <Button
              variant={errPane ? 'default' : 'ghost'}
              size="sm"
              onClick={() => setErrPane((v) => !v)}
            >
              <Activity size={14} /> health
            </Button>
          </div>
        </div>
        <div className="flex items-center gap-1 overflow-x-auto border-t border-border px-4 py-2">
          {CYCLE.map((step, i) => (
            <div key={step.id} className="flex items-center gap-1">
              {i > 0 && <span className="px-1 text-faint">→</span>}
              <span
                className={cn(
                  'rounded-sm px-2 py-1 text-[13px] tracking-[0.08em]',
                  liveState === step.id
                    ? 'border border-accent bg-accent/15 text-accent'
                    : 'border border-transparent text-muted',
                )}
              >
                {step.label}
              </span>
            </div>
          ))}
        </div>
      </header>

      {flash && (
        <div className="border-b border-red-900 bg-red-950/40 px-[10px] py-1 text-red-300">{flash}</div>
      )}

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-[300px] shrink-0 flex-col border-r border-border bg-panel">
          <div className="border-b border-border px-3 py-2 text-[12px] tracking-[0.08em] text-muted">
            NEW · CORPORA
          </div>
          <form
            className="flex flex-col gap-2 border-b border-border p-3"
            onSubmit={(e) => {
              e.preventDefault()
              if (!title.trim()) return
              act(async () => {
                const t = await api<Ticket>('/api/tickets', {
                  method: 'POST',
                  body: JSON.stringify({ title, body, project: 'corpora' }),
                })
                setTitle('')
                setBody('')
                setSel(t.id)
              })
            }}
          >
            <input
              className="h-9 border border-border bg-bg px-2 text-[15px] text-fg outline-none focus:border-accent"
              placeholder="title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
            <textarea
              className="h-20 resize-none border border-border bg-bg p-2 text-[15px] text-fg outline-none focus:border-accent"
              placeholder="what should the factory do?"
              value={body}
              onChange={(e) => setBody(e.target.value)}
            />
            <Button type="submit" disabled={busy || !title.trim()}>
              <Plus size={14} /> file
            </Button>
          </form>
          <div className="border-b border-border px-3 py-2 text-[12px] tracking-[0.08em] text-muted">
            AGENT FEED
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2 font-mono text-[13px] leading-5">
            {feed.length === 0 && <p className="text-faint">waiting for a run…</p>}
            {feed.map((line, i) => (
              <div
                key={`${line.at}-${i}`}
                className={cn(
                  'mb-1 whitespace-pre-wrap break-words',
                  line.kind === 'stderr' ? 'text-red-400' : line.kind === 'cycle' ? 'text-accent' : 'text-fg',
                )}
              >
                <span className="text-faint">{line.at} </span>
                {line.text}
              </div>
            ))}
            <div ref={feedEnd} />
          </div>
        </aside>

        <main className="flex min-w-0 flex-1 flex-col">
          <div className="flex min-h-0 flex-1 overflow-x-auto">
            {COLS.map((col) => (
              <section
                key={col}
                className="flex w-[220px] shrink-0 flex-col border-r border-border"
              >
                <div className="border-b border-border px-3 py-2 text-[13px] tracking-[0.08em] text-muted">
                  {COL_LABEL[col]}{' '}
                  <span className="text-faint">{grouped[col]?.length ?? 0}</span>
                </div>
                <div className="flex flex-1 flex-col gap-1 overflow-y-auto p-2">
                  {(grouped[col] ?? []).map((t) => (
                    <button
                      key={t.id}
                      onClick={() => setSel(t.id)}
                      className={cn(
                        'px-3 py-2 text-left text-[15px]',
                        sel === t.id
                          ? 'border-l-2 border-accent bg-accent/15 text-accent'
                          : 'border-l-2 border-transparent bg-panel hover:bg-panel2',
                      )}
                    >
                      <div className="line-clamp-2">{t.title}</div>
                      <div className="mt-1 font-mono text-[12px] text-faint">{t.id}</div>
                    </button>
                  ))}
                </div>
              </section>
            ))}
          </div>
        </main>

        <aside className="flex w-[400px] shrink-0 flex-col border-l border-border bg-panel">
          <div className="border-b border-border px-3 py-2 text-[13px] tracking-[0.08em] text-muted">
            {detail ? detail.state : 'TICKET'}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-3">
            {!detail && <p className="text-faint">select a ticket</p>}
            {detail && (
              <div className="flex flex-col gap-3">
                <h2 className="text-[18px]">{detail.title}</h2>
                <p className="whitespace-pre-wrap text-[15px] text-muted">{detail.body}</p>
                <div className="flex flex-wrap gap-1">
                  {detail.state === 'inbox' && (
                    <Button size="sm" disabled={busy} onClick={() => act(() => api(`/api/tickets/${detail.id}/accept`, { method: 'POST' }))}>
                      accept
                    </Button>
                  )}
                  {detail.state === 'plan_review' && (
                    <Button size="sm" disabled={busy} onClick={() => act(() => api(`/api/tickets/${detail.id}/approve-plan`, { method: 'POST' }))}>
                      approve plan
                    </Button>
                  )}
                  {detail.state === 'merge_review' && (
                    <Button size="sm" disabled={busy} onClick={() => act(() => api(`/api/tickets/${detail.id}/approve-merge`, { method: 'POST' }))}>
                      approve merge
                    </Button>
                  )}
                </div>
                {plan && (
                  <section>
                    <div className="mb-1 text-[11px] tracking-[0.08em] text-muted">
                      PLAN v{plan.version}
                    </div>
                    <pre className="whitespace-pre-wrap border border-border bg-bg p-2 text-[14px] text-fg">
                      {plan.body}
                    </pre>
                    <textarea
                      className="mt-2 h-20 w-full border border-border bg-bg p-2 outline-none focus:border-accent"
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
                  <section>
                    <div className="mb-1 text-[11px] tracking-[0.08em] text-muted">RESULT</div>
                    <pre className="whitespace-pre-wrap border border-border bg-bg p-2 text-[12px]">
                      {result.body}
                    </pre>
                  </section>
                )}
                {detail.runs[0] && (
                  <section className="text-faint">
                    last run {detail.runs[0].stage} · {detail.runs[0].status}
                  </section>
                )}
              </div>
            )}
          </div>
        </aside>

        {errPane && (
          <aside className="flex w-[320px] shrink-0 flex-col border-l border-border bg-panel">
            <div className="border-b border-border px-[10px] py-[8px] text-[11px] tracking-[0.08em] text-muted">
              ERRORS
            </div>
            <div className="flex-1 overflow-y-auto">
              {errors.length === 0 && <p className="p-[10px] text-faint">clean</p>}
              {errors.map((e) => (
                <div key={e.id} className="border-b border-border px-[10px] py-[7px]">
                  <div className="text-[11px] text-accent">
                    {e.source} · {e.level}
                  </div>
                  <div>{e.message}</div>
                  <div className="text-[10px] text-faint">{e.at}</div>
                </div>
              ))}
            </div>
          </aside>
        )}
      </div>
    </div>
  )
}

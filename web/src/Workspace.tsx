import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ArrowLeft,
  ArrowUp,
  Box,
  CheckCircle2,
  Cloud,
  FileCode2,
  GitBranch,
  LogOut,
  Plus,
  RefreshCw,
  Rocket,
  XCircle,
} from 'lucide-react'
import { api, apiBase, authToken, AUTH_KEY, type Project } from './lib/api'
import { cn } from './lib/utils'

type Branch = { id: string; name: string; kind: string; head_sha: string }
type Snapshot = { id: string; sha: string; message: string; created_by: string; created_at: string }
type Deployment = {
  id: string
  instance_name: string
  production: number
  status: string
  url: string | null
  sha: string
  created_at: string
}
type EventRow = { id: number; type: string; payload: string | null; ticket_id: string | null; at: string }
type Me = { id: string; email: string; name: string; avatar_url: string; connections: { id: string; provider: string; external_id: string }[] }

type StreamEvt = { kind: string; text: string; at: string }

type ChatLine = {
  id: string
  role: 'user' | 'agent'
  text: string
  status?: string
  events?: StreamEvt[]
}

const TABS = ['chat', 'events', 'files', 'deploy'] as const
type Tab = (typeof TABS)[number]

function fmtTime(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function parsePayload(payload: string | null): Record<string, unknown> {
  if (!payload) return {}
  try {
    return JSON.parse(payload)
  } catch {
    return {}
  }
}

export default function Workspace({
  slug,
  onBack,
  onBoard,
}: {
  slug: string
  onBack: () => void
  onBoard: () => void
}) {
  const [proj, setProj] = useState<Project | null>(null)
  const [me, setMe] = useState<Me | null>(null)
  const [branches, setBranches] = useState<Branch[]>([])
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [deployments, setDeployments] = useState<Deployment[]>([])
  const [events, setEvents] = useState<EventRow[]>([])
  const [secrets, setSecrets] = useState<string[]>([])
  const [sandbox, setSandbox] = useState<{ status: string; preview_url: string; last_synced_sha: string }>({
    status: 'never',
    preview_url: '',
    last_synced_sha: '',
  })
  const [tab, setTab] = useState<Tab>('chat')
  const [lines, setLines] = useState<ChatLine[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [deploying, setDeploying] = useState(false)
  const [userOpen, setUserOpen] = useState(false)
  const [newBranch, setNewBranch] = useState('')
  const [branchOpen, setBranchOpen] = useState(false)
  const [newSnapMsg, setNewSnapMsg] = useState('')
  const [snapOpen, setSnapOpen] = useState(false)
  const [newSecret, setNewSecret] = useState<{ name: string; value: string } | null>(null)
  const endRef = useRef<HTMLDivElement>(null)
  const busyRef = useRef(false)
  busyRef.current = busy

  const refresh = useCallback(async () => {
    const p = await api<Project>(`/api/projects/${slug}`)
    setProj(p)
    setBranches(await api<Branch[]>(`/api/projects/${slug}/branches`))
    setSnapshots(await api<Snapshot[]>(`/api/projects/${slug}/snapshots`))
    setDeployments(await api<Deployment[]>(`/api/projects/${slug}/deployments`))
    setEvents((await api<EventRow[]>(`/api/projects/${slug}/events`)).slice(0, 40))
    try {
      setSandbox(await api<typeof sandbox>(`/api/projects/${slug}/sandbox`))
    } catch {
      /* sandbox may be unprovisioned */
    }
    try {
      const s = await api<{ path: string }[]>(`/api/projects/${slug}/files?prefix=&store=vault`)
      setSecrets(s.map((e) => e.path))
    } catch {
      /* vault may be empty */
    }
  }, [slug])

  useEffect(() => {
    refresh().catch(() => setNotice('failed to load project'))
    const t = setInterval(() => {
      refresh().catch(() => undefined)
    }, 8000)
    return () => clearInterval(t)
  }, [refresh])

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [lines])

  // user menu
  useEffect(() => {
    api<Me>('/api/users/me')
      .then(setMe)
      .catch(() => setMe(null))
  }, [])

  // live agent stream: capture events while a turn is in flight
  useEffect(() => {
    const tok = authToken()
    const url = tok ? `${apiBase()}/api/stream?token=${encodeURIComponent(tok)}` : `${apiBase()}/api/stream`
    const es = new EventSource(url)
    es.onmessage = (ev) => {
      try {
        const item = JSON.parse(ev.data) as StreamEvt
        if (!busyRef.current) return
        if (!['think', 'token', 'tool', 'tool_result', 'usage', 'agent', 'stderr'].includes(item.kind)) return
        setLines((prev) => {
          const last = prev[prev.length - 1]
          if (!last || last.role !== 'agent' || !last.status) return prev
          return [
            ...prev.slice(0, -1),
            { ...last, events: [...(last.events || []), item] },
          ]
        })
      } catch {
        /* ignore malformed frames */
      }
    }
    return () => es.close()
  }, [])

  async function send() {
    const text = draft.trim()
    if (!text || busy) return
    setDraft('')
    setBusy(true)
    setLines((prev) => [
      ...prev,
      { id: `u-${Date.now()}`, role: 'user', text },
      { id: `a-${Date.now()}`, role: 'agent', text: '', status: 'running', events: [] },
    ])
    setTab('chat')
    try {
      const r = await api<{ ok?: boolean; mode: string; run?: { status?: string; stderr?: string | null; stdout?: string | null } }>(
        `/api/projects/${slug}/chat`,
        { method: 'POST', body: JSON.stringify({ text }) },
      )
      const summary =
        r.ok === false
          ? `failed — ${(r.run?.stderr || '').slice(0, 300) || 'agent error'}`
          : r.mode === 'tickets'
            ? 'filed as a ticket'
            : 'done'
      setLines((prev) => [
        ...prev.map((l) =>
          l.status === 'running'
            ? { ...l, status: undefined, text: summary || l.text }
            : l,
        ),
      ])
      refresh()
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'request failed'
      setLines((prev) => [
        ...prev.map((l) => (l.status === 'running' ? { ...l, status: undefined, text: msg } : l)),
      ])
    } finally {
      setBusy(false)
    }
  }

  async function ensureSandbox() {
    setNotice(null)
    try {
      await api(`/api/projects/${slug}/sandbox/ensure`, { method: 'POST' })
      await api(`/api/projects/${slug}/sandbox/sync`, { method: 'POST' })
      setNotice('sandbox warming up — preview appears when the dev server binds')
      refresh()
    } catch (e) {
      setNotice(e instanceof Error ? `sandbox failed: ${e.message}` : 'sandbox failed')
    }
  }

  async function sleepDestroy(action: 'sleep' | 'destroy') {
    setNotice(null)
    try {
      await api(`/api/projects/${slug}/sandbox/${action}`, { method: 'POST' })
      setNotice(`sandbox ${action}d`)
      refresh()
    } catch (e) {
      setNotice(e instanceof Error ? `${action} failed: ${e.message}` : `${action} failed`)
    }
  }

  async function createBranch() {
    const name = newBranch.trim()
    if (!name) return
    try {
      await api(`/api/projects/${slug}/branches`, { method: 'POST', body: JSON.stringify({ name, kind: 'feature' }) })
      setNewBranch('')
      setBranchOpen(false)
      refresh()
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'branch failed')
    }
  }

  async function createSnapshot() {
    const message = newSnapMsg.trim() || 'snapshot'
    try {
      await api(`/api/projects/${slug}/snapshots`, { method: 'POST', body: JSON.stringify({ message }) })
      setNewSnapMsg('')
      setSnapOpen(false)
      refresh()
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'snapshot failed')
    }
  }

  async function scaffold() {
    setNotice(null)
    try {
      const r = await api<{ files: number }>(`/api/projects/${slug}/scaffold`, {
        method: 'POST',
        body: JSON.stringify({ template: 'vite-react' }),
      })
      setNotice(`scaffolded vite-react — ${r.files} files`)
      refresh()
    } catch (e) {
      setNotice(e instanceof Error ? `scaffold failed: ${e.message}` : 'scaffold failed')
    }
  }

  async function deploy(instance: 'dev' | 'prod') {
    if (deploying) return
    setDeploying(true)
    setNotice(null)
    try {
      const d = await api<{ id: string; status: string; url?: string }>(
        `/api/projects/${slug}/deployments`,
        { method: 'POST', body: JSON.stringify({ instance }) },
      )
      setNotice(`deploy ${d.status}${d.url ? ` — ${d.url}` : ''}`)
      refresh()
    } catch (e) {
      setNotice(e instanceof Error ? `deploy failed: ${e.message}` : 'deploy failed')
    } finally {
      setDeploying(false)
    }
  }

  async function setSecret() {
    if (!newSecret?.name.trim()) return
    try {
      await api(`/api/projects/${slug}/files/${newSecret.name.trim()}?store=vault`, {
        method: 'PUT',
        body: JSON.stringify({ body: newSecret.value, store: 'vault' }),
      })
      setNewSecret(null)
      refresh()
    } catch (e) {
      setNotice(e instanceof Error ? `secret failed: ${e.message}` : 'secret failed')
    }
  }

  async function deleteSecret(name: string) {
    try {
      await api(`/api/projects/${slug}/files/${name}?store=vault`, { method: 'DELETE' })
      refresh()
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'delete failed')
    }
  }

  function logout() {
    localStorage.removeItem(AUTH_KEY)
    window.location.hash = '#/'
    window.location.reload()
  }

  const prodDeployment = deployments.find((d) => d.production === 1 && d.status === 'ready')
  const devUrl = sandbox.preview_url || ''
  const sandboxUp = ['running', 'provisioning'].includes(sandbox.status)

  return (
    <div className="flex h-full flex-col bg-bg text-fg">
      {/* header */}
      <header className="flex h-10 shrink-0 items-center gap-2 border-b border-border bg-panel px-3">
        <button
          type="button"
          onClick={onBack}
          className="inline-flex h-7 w-7 items-center justify-center text-faint hover:text-accent"
          title="back to projects"
        >
          <ArrowLeft size={15} />
        </button>
        <span className="flex-1 truncate text-[13px] tracking-[0.08em] text-fg">
          {proj?.name || slug}
        </span>
        <span
          className={cn(
            'rounded-sm border px-2 py-0.5 text-[10px] tracking-[0.12em] uppercase',
            proj?.mode === 'tickets' ? 'border-border-hi text-muted' : 'border-accent/40 text-accent',
          )}
          title={proj?.quality_gates ? 'tickets + quality gates' : undefined}
        >
          {proj?.mode || 'live'}
        </span>
        <button
          type="button"
          onClick={onBoard}
          className="inline-flex h-7 items-center rounded-sm border border-border px-2 text-[11px] text-faint hover:border-border-hi hover:text-fg"
          title="board view (tickets)"
        >
          board
        </button>
        <button
          type="button"
          onClick={refresh}
          className="inline-flex h-7 w-7 items-center justify-center text-faint hover:text-accent"
          title="refresh"
        >
          <RefreshCw size={14} />
        </button>
        {me && (
          <div className="relative">
            <button
              type="button"
              onClick={() => setUserOpen((v) => !v)}
              className="inline-flex h-7 items-center gap-1.5 rounded-sm border border-border px-2 text-[11px] text-faint hover:border-border-hi hover:text-fg"
              title="account"
            >
              {me.avatar_url ? (
                <img src={me.avatar_url} alt="" className="h-4 w-4 rounded-full" />
              ) : (
                <span className="inline-flex h-4 w-4 items-center justify-center rounded-full bg-accent/20 text-[9px] text-accent">
                  {(me.name || me.email || '?').slice(0, 1).toUpperCase()}
                </span>
              )}
              <span className="max-w-[120px] truncate">{me.name || me.email}</span>
            </button>
            {userOpen && (
              <div className="absolute right-0 top-8 z-20 w-64 border border-border bg-panel p-3 shadow-xl">
                <div className="mb-2 text-[11px] tracking-[0.1em] text-faint uppercase">connections</div>
                {me.connections.length === 0 && <p className="text-[12px] text-faint">none connected</p>}
                {me.connections.map((c) => (
                  <div key={c.id} className="flex items-center gap-2 py-1 text-[12px] text-muted">
                    <Cloud size={12} className="text-faint" />
                    <span>{c.provider}</span>
                    {c.external_id && <span className="truncate text-faint">{c.external_id}</span>}
                  </div>
                ))}
                <button
                  type="button"
                  onClick={logout}
                  className="mt-3 inline-flex items-center gap-1.5 text-[12px] text-faint hover:text-red-400"
                >
                  <LogOut size={12} /> sign out
                </button>
              </div>
            )}
          </div>
        )}
      </header>

      {notice && (
        <div className="flex shrink-0 items-center gap-2 border-b border-border bg-panel2 px-3 py-1.5 text-[12px] text-muted">
          <span className="flex-1 truncate">{notice}</span>
          <button type="button" onClick={() => setNotice(null)} className="text-faint hover:text-fg">
            <XCircle size={13} />
          </button>
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        {/* left rail */}
        <aside className="flex w-60 shrink-0 flex-col border-r border-border bg-panel">
          <section className="min-h-0 flex-1 overflow-y-auto p-2">
            <div className="mb-1 flex items-center justify-between px-1">
              <span className="text-[10px] tracking-[0.14em] text-faint uppercase">branches</span>
              <button
                type="button"
                onClick={() => setBranchOpen((v) => !v)}
                className="text-faint hover:text-accent"
                title="create branch"
              >
                <Plus size={12} />
              </button>
            </div>
            {branchOpen && (
              <form
                className="mb-1 flex gap-1"
                onSubmit={(e) => {
                  e.preventDefault()
                  createBranch()
                }}
              >
                <input
                  autoFocus
                  className="h-7 min-w-0 flex-1 border border-border bg-bg px-1.5 text-[12px] outline-none focus:border-accent"
                  placeholder="feature-name"
                  value={newBranch}
                  onChange={(e) => setNewBranch(e.target.value)}
                />
              </form>
            )}
            {branches.map((b) => (
              <div
                key={b.id}
                className="flex items-center gap-2 rounded-sm px-1.5 py-1 text-[12px] text-muted"
              >
                <GitBranch size={12} className="shrink-0 text-faint" />
                <span className="flex-1 truncate">{b.name}</span>
                {b.head_sha && <span className="text-[10px] text-faint">{b.head_sha.slice(0, 7)}</span>}
              </div>
            ))}

            <div className="mb-1 mt-4 flex items-center justify-between px-1">
              <span className="text-[10px] tracking-[0.14em] text-faint uppercase">snapshots</span>
              <button
                type="button"
                onClick={() => setSnapOpen((v) => !v)}
                className="text-faint hover:text-accent"
                title="create snapshot"
              >
                <Box size={12} />
              </button>
            </div>
            {snapOpen && (
              <form
                className="mb-1 flex gap-1"
                onSubmit={(e) => {
                  e.preventDefault()
                  createSnapshot()
                }}
              >
                <input
                  autoFocus
                  className="h-7 min-w-0 flex-1 border border-border bg-bg px-1.5 text-[12px] outline-none focus:border-accent"
                  placeholder="message"
                  value={newSnapMsg}
                  onChange={(e) => setNewSnapMsg(e.target.value)}
                />
              </form>
            )}
            {snapshots.length === 0 && (
              <p className="px-1.5 py-1 text-[11px] text-faint">no snapshots yet — deploy or save one</p>
            )}
            {snapshots.slice(0, 8).map((s) => (
              <div key={s.id} className="flex items-center gap-2 rounded-sm px-1.5 py-1 text-[12px] text-muted">
                <Box size={12} className="shrink-0 text-faint" />
                <span className="flex-1 truncate">{s.message || s.sha.slice(0, 7)}</span>
                <span className="text-[10px] text-faint">{fmtTime(s.created_at)}</span>
              </div>
            ))}

            <div className="mb-1 mt-4 px-1 text-[10px] tracking-[0.14em] text-faint uppercase">deploys</div>
            {deployments.length === 0 && (
              <p className="px-1.5 py-1 text-[11px] text-faint">none yet</p>
            )}
            {deployments.slice(0, 6).map((d) => (
              <div key={d.id} className="flex items-center gap-2 rounded-sm px-1.5 py-1 text-[12px] text-muted">
                {d.status === 'ready' ? (
                  <CheckCircle2 size={12} className="shrink-0 text-accent" />
                ) : d.status === 'failed' ? (
                  <XCircle size={12} className="shrink-0 text-red-400" />
                ) : (
                  <Cloud size={12} className="shrink-0 text-faint" />
                )}
                <span className="flex-1 truncate">{d.instance_name}</span>
                <span className="text-[10px] text-faint">{d.status}</span>
              </div>
            ))}
          </section>

          <footer className="flex shrink-0 flex-col gap-1 border-t border-border p-2">
            {devUrl && (
              <a
                href={devUrl}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-2 rounded-sm border border-border px-2 py-1.5 text-[11px] text-muted hover:border-accent/50 hover:text-fg"
              >
                <Rocket size={12} className="text-accent" />
                <span className="flex-1 truncate">open preview</span>
              </a>
            )}
            {prodDeployment?.url && (
              <a
                href={prodDeployment.url}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-2 rounded-sm border border-border px-2 py-1.5 text-[11px] text-muted hover:border-accent/50 hover:text-fg"
              >
                <Cloud size={12} className="text-accent" />
                <span className="flex-1 truncate">{prodDeployment.url}</span>
              </a>
            )}
          </footer>
        </aside>

        {/* center: preview */}
        <main className="flex min-w-0 flex-1 flex-col bg-bg">
          <div className="flex h-8 shrink-0 items-center gap-2 border-b border-border px-3">
            <span className="text-[10px] tracking-[0.14em] text-faint uppercase">preview</span>
            <span className="flex-1 truncate text-[11px] text-faint">
              {devUrl || sandbox.status === 'never' ? 'not provisioned' : `sandbox ${sandbox.status}`}
            </span>
            {!sandboxUp && (
              <button
                type="button"
                onClick={ensureSandbox}
                className="inline-flex h-6 items-center gap-1 rounded-sm border border-accent/40 px-2 text-[11px] text-accent hover:bg-accent/10"
              >
                <Rocket size={11} />
                start sandbox
              </button>
            )}
            {sandboxUp && (
              <>
                <button
                  type="button"
                  onClick={() => sleepDestroy('sleep')}
                  className="inline-flex h-6 items-center rounded-sm border border-border px-2 text-[11px] text-faint hover:border-border-hi hover:text-fg"
                  title="sleep sandbox"
                >
                  sleep
                </button>
                <button
                  type="button"
                  onClick={() => sleepDestroy('destroy')}
                  className="inline-flex h-6 items-center rounded-sm border border-border px-2 text-[11px] text-faint hover:border-red-500/50 hover:text-red-400"
                  title="destroy sandbox"
                >
                  destroy
                </button>
              </>
            )}
            <button
              type="button"
              onClick={scaffold}
              className="inline-flex h-6 items-center gap-1 rounded-sm border border-border px-2 text-[11px] text-faint hover:border-accent/50 hover:text-fg"
              title="scaffold the vite-react template"
            >
              <Plus size={11} /> scaffold
            </button>
          </div>
          <div className="min-h-0 flex-1 bg-panel2">
            {devUrl ? (
              <iframe
                key={devUrl}
                src={devUrl}
                title="app preview"
                className="h-full w-full border-0"
                sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
              />
            ) : (
              <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
                <div className="text-[13px] text-faint">
                  {sandbox.status === 'never' || sandbox.status === 'destroyed'
                    ? 'Provision a sandbox to run the app live.'
                    : 'Sandbox provisioning… the preview iframe appears when the dev server binds.'}
                </div>
                <button
                  type="button"
                  onClick={ensureSandbox}
                  className="rounded-sm border border-border px-3 py-1.5 text-[12px] text-muted hover:border-accent/50 hover:text-fg"
                >
                  provision
                </button>
              </div>
            )}
          </div>
        </main>

        {/* right panel: tabs */}
        <aside className="flex w-[380px] shrink-0 flex-col border-l border-border bg-panel">
          <nav className="flex h-9 shrink-0 items-stretch border-b border-border">
            {TABS.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTab(t)}
                className={cn(
                  'flex-1 border-b-2 text-[11px] tracking-[0.12em] uppercase',
                  tab === t
                    ? 'border-accent bg-accent/10 text-accent'
                    : 'border-transparent text-faint hover:text-fg',
                )}
              >
                {t}
              </button>
            ))}
          </nav>

          {tab === 'chat' && (
            <>
              <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
                {lines.length === 0 && (
                  <p className="text-[13px] leading-6 text-faint">
                    Direct the factory. Live mode: the agent edits the repo and the preview
                    updates. Tickets mode files work items instead.
                  </p>
                )}
                {lines.map((l) => (
                  <div key={l.id} className="mb-3">
                    <div className="mb-0.5 text-[10px] tracking-[0.12em] uppercase text-faint">
                      {l.role}
                    </div>
                    {l.role === 'user' ? (
                      <div className="text-[13px] leading-6 text-fg">{l.text}</div>
                    ) : (
                      <div className="text-[13px] leading-6">
                        {l.status === 'running' && l.events?.length === 0 && (
                          <span className="text-accent2">agent working…</span>
                        )}
                        {(l.events || []).map((e, i) => (
                          <div
                            key={i}
                            className={cn(
                              'whitespace-pre-wrap break-words',
                              e.kind === 'think'
                                ? 'text-muted italic'
                                : e.kind === 'tool'
                                  ? 'text-accent'
                                  : e.kind === 'tool_result'
                                    ? 'text-muted'
                                    : e.kind === 'usage'
                                      ? 'text-faint'
                                      : e.kind === 'stderr'
                                        ? 'text-red-400'
                                        : 'text-fg',
                            )}
                          >
                            {e.kind === 'tool' && <span className="mr-1 text-faint">⚙</span>}
                            {e.kind === 'tool_result' && <span className="mr-1 text-faint">↩</span>}
                            {e.text}
                          </div>
                        ))}
                        {l.status === 'running' && (l.events?.length || 0) > 0 && (
                          <span className="ml-0.5 inline-block h-[1em] w-[0.5ch] animate-pulse bg-accent align-text-bottom" />
                        )}
                        {!l.status && l.text && <div className="text-muted">{l.text}</div>}
                      </div>
                    )}
                  </div>
                ))}
                <div ref={endRef} />
              </div>
              <form
                className="flex shrink-0 items-end gap-2 border-t border-border px-3 py-2"
                onSubmit={(e) => {
                  e.preventDefault()
                  send()
                }}
              >
                <textarea
                  className="max-h-32 min-h-[36px] flex-1 resize-none border-0 bg-transparent py-2 text-[14px] leading-5 outline-none placeholder:text-faint"
                  rows={1}
                  placeholder="build something…"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      send()
                    }
                  }}
                />
                <button
                  type="submit"
                  disabled={!draft.trim() || busy}
                  className="mb-1 inline-flex h-8 w-8 items-center justify-center text-accent disabled:text-faint"
                  title="send"
                >
                  <ArrowUp size={16} />
                </button>
              </form>
            </>
          )}

          {tab === 'events' && (
            <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
              {events.length === 0 && <p className="py-2 text-[12px] text-faint">no events yet</p>}
              {events.map((e) => {
                const p = parsePayload(e.payload)
                const detail =
                  typeof p.message === 'string' ? p.message : p.stage ? String(p.stage) : ''
                return (
                  <div key={e.id} className="border-b border-border/60 py-1.5">
                    <div className="flex items-center gap-2 text-[11px]">
                      <span className="text-faint">{fmtTime(e.at)}</span>
                      <span className="text-accent">{e.type}</span>
                      {e.ticket_id && <span className="truncate text-faint">{e.ticket_id}</span>}
                    </div>
                    {detail && <div className="mt-0.5 text-[12px] text-muted">{detail}</div>}
                  </div>
                )
              })}
            </div>
          )}

          {tab === 'files' && (
            <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
              <p className="py-2 text-[12px] leading-5 text-faint">
                File tree lives in the repo store. The agent edits these files; snapshots make
                deploy points immutable.
              </p>
              <button
                type="button"
                onClick={onBoard}
                className="mt-1 inline-flex items-center gap-1.5 text-[12px] text-accent hover:text-accent2"
              >
                <FileCode2 size={13} />
                open inspector (board view)
              </button>
            </div>
          )}

          {tab === 'deploy' && (
            <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
              <p className="mb-3 text-[12px] leading-5 text-faint">
                Deploying copies current state to an immutable snapshot, then provisions the target.
                Dev = sandbox preview. Prod = Pages under your Cloudflare account.
              </p>
              <button
                type="button"
                onClick={() => deploy('dev')}
                disabled={deploying}
                className="mb-2 flex w-full items-center justify-center gap-2 rounded-sm border border-accent/40 py-2 text-[12px] text-accent hover:bg-accent/10 disabled:opacity-40"
              >
                <Rocket size={13} />
                deploy dev
              </button>
              <button
                type="button"
                onClick={() => deploy('prod')}
                disabled={deploying}
                className="flex w-full items-center justify-center gap-2 rounded-sm border border-border py-2 text-[12px] text-muted hover:border-accent/50 hover:text-fg disabled:opacity-40"
                title="production — human-gated"
              >
                <Cloud size={13} />
                deploy production
              </button>
              <div className="mt-4">
                <div className="mb-1 text-[10px] tracking-[0.14em] text-faint uppercase">history</div>
                {deployments.length === 0 && <p className="text-[12px] text-faint">none yet</p>}
                {deployments.map((d) => (
                  <div key={d.id} className="flex items-center gap-2 border-b border-border/60 py-1.5 text-[12px]">
                    <span className="w-14 shrink-0 text-faint">{d.instance_name}</span>
                    <span className={cn('flex-1', d.status === 'failed' ? 'text-red-400' : 'text-muted')}>
                      {d.status}
                    </span>
                    <span className="text-[10px] text-faint">{d.sha.slice(0, 7)}</span>
                  </div>
                ))}
              </div>

              <div className="mt-5">
                <div className="mb-1 flex items-center justify-between">
                  <span className="text-[10px] tracking-[0.14em] text-faint uppercase">secrets</span>
                  <button
                    type="button"
                    onClick={() => setNewSecret({ name: '', value: '' })}
                    className="text-faint hover:text-accent"
                    title="add secret"
                  >
                    <Plus size={12} />
                  </button>
                </div>
                {newSecret && (
                  <div className="mb-2 flex flex-col gap-1 border border-border p-2">
                    <input
                      autoFocus
                      className="h-7 border border-border bg-bg px-1.5 text-[12px] outline-none focus:border-accent"
                      placeholder="NAME (vault)"
                      value={newSecret.name}
                      onChange={(e) => setNewSecret({ ...newSecret, name: e.target.value })}
                    />
                    <input
                      className="h-7 border border-border bg-bg px-1.5 text-[12px] outline-none focus:border-accent"
                      placeholder="value"
                      type="password"
                      value={newSecret.value}
                      onChange={(e) => setNewSecret({ ...newSecret, value: e.target.value })}
                    />
                    <button
                      type="button"
                      onClick={setSecret}
                      disabled={!newSecret.name.trim()}
                      className="self-start border border-accent/40 px-2 py-0.5 text-[11px] text-accent hover:bg-accent/10 disabled:opacity-40"
                    >
                      set
                    </button>
                  </div>
                )}
                {secrets.length === 0 && !newSecret && (
                  <p className="text-[12px] text-faint">no secrets — deploy env vars land here</p>
                )}
                {secrets.map((s) => (
                  <div key={s} className="flex items-center gap-2 border-b border-border/60 py-1.5 text-[12px]">
                    <span className="flex-1 truncate font-mono text-muted">{s}</span>
                    <button
                      type="button"
                      onClick={() => deleteSecret(s)}
                      className="text-faint hover:text-red-400"
                      title="delete secret"
                    >
                      <XCircle size={12} />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </aside>
      </div>
    </div>
  )
}

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Action, Field, Section, Tabs } from './flat'
import { CollapsedStrip, CollapseBtn } from './Rail'
import {
  api,
  apiBase,
  authToken,
  type FileEntry,
  type Project,
  type ProjectApp,
  type TreeFile,
} from './lib/api'
import { cn } from './lib/utils'

export type InspectorFile = { path: string; ref: string }

export type InspectorCtx = {
  slug: string
  project: Project | null
  file: InspectorFile | null
  onFile: (file: InspectorFile | null) => void
  onAssetsChanged: () => void
  errors: { id: number; source: string; level: string; message: string; at: string }[]
}

export type InspectorTab = {
  id: string
  label: string
  pane: (ctx: InspectorCtx) => ReactNode
}

/** Append here when a new inspector concern appears. */
export const INSPECTOR_TABS: InspectorTab[] = [
  { id: 'file', label: 'file', pane: (ctx) => <FilePane ctx={ctx} /> },
  { id: 'apps', label: 'apps', pane: (ctx) => <AppsPane ctx={ctx} /> },
  { id: 'assets', label: 'assets', pane: (ctx) => <AssetsPane ctx={ctx} /> },
  { id: 'settings', label: 'settings', pane: (ctx) => <SettingsPane ctx={ctx} /> },
]

export function Inspector({
  tab,
  onTab,
  ctx,
  open,
  onToggle,
}: {
  tab: string
  onTab: (id: string) => void
  ctx: InspectorCtx
  open: boolean
  onToggle: () => void
}) {
  const active = INSPECTOR_TABS.find((t) => t.id === tab) ?? INSPECTOR_TABS[0]
  if (!open) {
    return <CollapsedStrip title={active.label} onToggle={onToggle} />
  }
  return (
    <aside className="inspector flex shrink-0 flex-col border-l border-border bg-panel">
      <div className="flex items-center border-b border-border">
        <CollapseBtn open={open} onToggle={onToggle} label="inspector" />
        <div className="min-w-0 flex-1">
          <Tabs items={INSPECTOR_TABS} value={active.id} onChange={onTab} />
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">{active.pane(ctx)}</div>
    </aside>
  )
}

function FilePane({ ctx }: { ctx: InspectorCtx }) {
  const [doc, setDoc] = useState<TreeFile | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!ctx.file) {
      setDoc(null)
      setErr(null)
      return
    }
    let gone = false
    setLoading(true)
    setErr(null)
    api<TreeFile>(
      `/api/projects/${ctx.slug}/tree/${ctx.file.path}?ref=${encodeURIComponent(ctx.file.ref)}`,
    )
      .catch(async () => {
        const f = await api<{ path: string; body?: string }>(
          `/api/projects/${ctx.slug}/files/${ctx.file!.path}?store=repo`,
        )
        const body = f.body ?? ''
        return {
          path: f.path,
          ref: 'asset',
          kind: extKind(f.path),
          mime: 'text/plain',
          bytes: body.length,
          binary: false,
          body,
        } satisfies TreeFile
      })
      .then((d) => {
        if (!gone) setDoc(d)
      })
      .catch((e) => {
        if (!gone) {
          setDoc(null)
          setErr(String(e))
        }
      })
      .finally(() => {
        if (!gone) setLoading(false)
      })
    return () => {
      gone = true
    }
  }, [ctx.slug, ctx.file?.path, ctx.file?.ref])

  if (!ctx.file) {
    return (
      <div className="px-4 py-8 text-[14px] text-faint">
        Pick a file in the tree. It renders here.
      </div>
    )
  }

  return (
    <FileRenderer
      slug={ctx.slug}
      file={ctx.file}
      doc={doc}
      loading={loading}
      err={err}
    />
  )
}

function FileRenderer({
  slug,
  file,
  doc,
  loading,
  err,
}: {
  slug: string
  file: InspectorFile
  doc: TreeFile | null
  loading: boolean
  err: string | null
}) {
  const kind = doc?.kind || extKind(file.path)
  return (
    <div className="flex min-h-full flex-col">
      <div className="border-b border-border px-4 py-3">
        <div className="truncate font-mono text-[13px] text-fg">{file.path}</div>
        <div className="mt-1 text-[12px] tracking-[0.06em] text-faint">
          {kind}
          {doc ? ` · ${fmtBytes(doc.bytes)}` : ''}
          {file.ref && file.ref !== 'HEAD' ? ` · ${file.ref}` : ''}
        </div>
      </div>
      {err && <div className="px-4 py-3 text-[13px] text-red-400">{err}</div>}
      {loading && <div className="px-4 py-6 text-[13px] text-faint">loading</div>}
      {!loading && !err && doc && <RendererBody slug={slug} file={file} doc={doc} />}
    </div>
  )
}

function RendererBody({ slug, file, doc }: { slug: string; file: InspectorFile; doc: TreeFile }) {
  if (doc.kind === 'image') {
    return <ImageBody slug={slug} path={file.path} gitRef={file.ref} />
  }
  if (doc.kind === 'binary' || doc.body == null) {
    return (
      <div className="px-4 py-8 text-[14px] text-faint">
        binary · {fmtBytes(doc.bytes)} · {doc.mime}
      </div>
    )
  }
  if (doc.kind === 'markdown') {
    return <MarkdownBody text={doc.body} />
  }
  if (doc.kind === 'json') {
    return (
      <pre className="overflow-x-auto px-4 py-3 font-mono text-[12px] leading-5 text-fg">
        {prettyJson(doc.body)}
      </pre>
    )
  }
  return (
    <pre className="overflow-x-auto px-4 py-3 font-mono text-[12px] leading-5 text-fg whitespace-pre-wrap">
      {doc.body}
    </pre>
  )
}

function ImageBody({ slug, path, gitRef }: { slug: string; path: string; gitRef: string }) {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    let obj: string | null = null
    let gone = false
    const headers: Record<string, string> = {}
    const tok = authToken()
    if (tok) headers.Authorization = `Bearer ${tok}`
    fetch(
      `${apiBase()}/api/projects/${slug}/blob/${path}?ref=${encodeURIComponent(gitRef)}`,
      { headers },
    )
      .then((r) => {
        if (!r.ok) throw new Error(r.statusText)
        return r.blob()
      })
      .then((b) => {
        obj = URL.createObjectURL(b)
        if (!gone) setUrl(obj)
      })
      .catch(() => {
        if (!gone) setUrl(null)
      })
    return () => {
      gone = true
      if (obj) URL.revokeObjectURL(obj)
    }
  }, [slug, path, gitRef])
  if (!url) return <div className="px-4 py-8 text-[13px] text-faint">image</div>
  return <img src={url} alt={path} className="max-w-full p-4" />
}

function MarkdownBody({ text }: { text: string }) {
  const blocks = useMemo(() => splitMd(text), [text])
  return (
    <div className="space-y-3 px-4 py-3 text-[14px] leading-6">
      {blocks.map((b, i) => {
        if (b.type === 'code') {
          return (
            <pre key={i} className="overflow-x-auto border-l border-border pl-3 font-mono text-[12px] leading-5">
              {b.text}
            </pre>
          )
        }
        if (b.type === 'h') {
          return (
            <h4 key={i} className="text-[15px] tracking-wide text-accent">
              {b.text}
            </h4>
          )
        }
        if (b.type === 'li') {
          return (
            <div key={i} className="pl-3 text-muted">
              · {b.text}
            </div>
          )
        }
        return (
          <p key={i} className="whitespace-pre-wrap text-fg">
            {b.text}
          </p>
        )
      })}
    </div>
  )
}

function AssetsPane({ ctx }: { ctx: InspectorCtx }) {
  const [store, setStore] = useState<'repo' | 'vault'>('repo')
  const [list, setList] = useState<FileEntry[]>([])
  const [flash, setFlash] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [path, setPath] = useState('')
  const [drag, setDrag] = useState(false)

  const load = useCallback(async () => {
    const rows = await api<FileEntry[]>(`/api/projects/${ctx.slug}/files?store=${store}`)
    setList(rows)
  }, [ctx.slug, store])

  useEffect(() => {
    load().catch((e) => setFlash(String(e)))
  }, [load])

  async function upload(file: File) {
    setBusy(true)
    setFlash(null)
    try {
      const dest = path.trim() || file.name.replace(/\\/g, '/')
      const fd = new FormData()
      fd.append('file', file)
      fd.append('path', dest)
      fd.append('store', store)
      const tok = authToken()
      const res = await fetch(`${apiBase()}/api/projects/${ctx.slug}/files/upload`, {
        method: 'POST',
        headers: tok ? { Authorization: `Bearer ${tok}` } : undefined,
        body: fd,
      })
      if (!res.ok) throw new Error(await res.text())
      setPath('')
      await load()
      ctx.onAssetsChanged()
    } catch (e) {
      setFlash(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function remove(p: string) {
    setBusy(true)
    setFlash(null)
    try {
      await api(`/api/projects/${ctx.slug}/files/${p}?store=${store}`, { method: 'DELETE' })
      await load()
      ctx.onAssetsChanged()
    } catch (e) {
      setFlash(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <Section title="upload" hint="Lands in the project asset store. Repo store also writes the checkout.">
        <div className="flex gap-2">
          <button
            type="button"
            className={cn('text-[12px] tracking-[0.1em] uppercase', store === 'repo' ? 'text-accent' : 'text-faint')}
            onClick={() => setStore('repo')}
          >
            repo
          </button>
          <button
            type="button"
            className={cn('text-[12px] tracking-[0.1em] uppercase', store === 'vault' ? 'text-accent' : 'text-faint')}
            onClick={() => setStore('vault')}
          >
            vault
          </button>
        </div>
        <Field
          placeholder="optional path (docs/brief.md)"
          value={path}
          onChange={(e) => setPath(e.target.value)}
        />
        <label
          className={cn(
            'flex cursor-pointer flex-col items-center justify-center border border-dashed border-border py-6 text-[13px] text-faint',
            drag && 'border-accent text-accent',
          )}
          onDragOver={(e) => {
            e.preventDefault()
            setDrag(true)
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDrag(false)
            const f = e.dataTransfer.files[0]
            if (f) void upload(f)
          }}
        >
          drop a file or browse
          <input
            type="file"
            className="hidden"
            disabled={busy}
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) void upload(f)
              e.target.value = ''
            }}
          />
        </label>
        {flash && <p className="text-[13px] text-red-400">{flash}</p>}
      </Section>
      <Section title="library" hint={`${list.length} ${store}`}>
        {list.length === 0 && <p className="text-[13px] text-faint">nothing stored yet</p>}
        {list.map((f) => (
          <div key={f.path} className="flex items-baseline gap-2 border-b border-border py-2">
            <button
              type="button"
              className="min-w-0 flex-1 truncate text-left font-mono text-[13px] hover:text-accent"
              onClick={() => ctx.onFile({ path: f.path, ref: 'HEAD' })}
            >
              {f.path}
            </button>
            <Action tone="danger" disabled={busy} onClick={() => remove(f.path)}>
              delete
            </Action>
          </div>
        ))}
      </Section>
    </div>
  )
}

function AppsPane({ ctx }: { ctx: InspectorCtx }) {
  const [items, setItems] = useState<ProjectApp[]>([])
  const [busy, setBusy] = useState(false)
  const [flash, setFlash] = useState<string | null>(null)
  const [need, setNeed] = useState<string | null>(null)
  const [token, setToken] = useState('')
  const [repos, setRepos] = useState<{ full_name: string; default_branch: string }[]>([])
  const [accounts, setAccounts] = useState<{ id: string; name: string }[]>([])
  const [pages, setPages] = useState<{ name: string }[]>([])
  const [buckets, setBuckets] = useState<{ name: string }[]>([])

  const load = useCallback(async () => {
    const rows = await api<ProjectApp[]>(`/api/projects/${ctx.slug}/apps`)
    setItems(rows)
  }, [ctx.slug])

  useEffect(() => {
    load().catch((e) => setFlash(String(e)))
  }, [load])

  async function act(fn: () => Promise<void>) {
    setBusy(true)
    setFlash(null)
    try {
      await fn()
    } catch (e) {
      setFlash(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function connect(id: string, extra?: string) {
    await act(async () => {
      const tok = authToken()
      const res = await fetch(`${apiBase()}/api/projects/${ctx.slug}/apps/${id}/install`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(tok ? { Authorization: `Bearer ${tok}` } : {}),
        },
        body: JSON.stringify({ token: extra ?? token }),
      })
      const body = await res.json().catch(() => ({}))
      if (res.status === 409) {
        setNeed(id)
        return
      }
      if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : res.statusText)
      if (body.url) {
        sessionStorage.setItem('factory-tab', 'apps')
        window.location.href = body.url
        return
      }
      setNeed(null)
      setToken('')
      await load()
      ctx.onAssetsChanged()
      if (id === 'github') await loadGithub()
      if (id === 'cloudflare') await loadCf()
    })
  }

  async function loadGithub() {
    const r = await api<{ repos: { full_name: string; default_branch: string }[] }>(
      `/api/projects/${ctx.slug}/apps/github/resources`,
    )
    setRepos(r.repos || [])
  }

  async function loadCf() {
    const r = await api<{
      accounts: { id: string; name: string }[]
      pages: { name: string }[]
      buckets: { name: string }[]
    }>(`/api/projects/${ctx.slug}/apps/cloudflare/resources`)
    setAccounts(r.accounts || [])
    setPages(r.pages || [])
    setBuckets(r.buckets || [])
  }

  return (
    <div>
      {flash && <div className="px-4 py-2 text-[13px] text-red-400">{flash}</div>}
      {items.map((app) => (
        <Section
          key={app.id}
          title={app.title}
          hint={app.installed ? app.identity || 'connected' : app.summary}
        >
          {!app.installed && (
            <>
              <p className="text-[13px] text-faint">{app.summary}</p>
              {need === app.id && (
                <Field
                  label="authorize"
                  type="password"
                  placeholder={app.id === 'github' ? 'github token' : 'cloudflare api token'}
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                />
              )}
              <Action
                tone="primary"
                disabled={busy || (need === app.id && !token.trim())}
                onClick={() => connect(app.id)}
              >
                {need === app.id ? 'authorize' : 'connect'}
              </Action>
            </>
          )}
          {app.installed && app.id === 'github' && (
            <>
              <p className="text-[13px]">
                {app.resource.repo || 'no repository linked'}
                {app.identity ? ` · ${app.identity}` : ''}
              </p>
              {repos.length > 0 && (
                <select
                  className="flat-input"
                  value={app.resource.repo || ''}
                  onChange={(e) => {
                    const repo = e.target.value
                    if (!repo) return
                    act(async () => {
                      await api(`/api/projects/${ctx.slug}/apps/github/bind`, {
                        method: 'POST',
                        body: JSON.stringify({ repo }),
                      })
                      await load()
                    })
                  }}
                >
                  <option value="">choose a repository</option>
                  {repos.map((r) => (
                    <option key={r.full_name} value={r.full_name}>
                      {r.full_name}
                    </option>
                  ))}
                </select>
              )}
              <div className="flex gap-2">
                <Action disabled={busy} onClick={() => act(loadGithub)}>
                  {repos.length ? 'refresh' : 'choose repo'}
                </Action>
                <Action
                  tone="danger"
                  disabled={busy}
                  onClick={() =>
                    act(async () => {
                      await api(`/api/projects/${ctx.slug}/apps/github/uninstall`, { method: 'POST' })
                      setRepos([])
                      await load()
                    })
                  }
                >
                  disconnect
                </Action>
              </div>
            </>
          )}
          {app.installed && app.id === 'cloudflare' && (
            <>
              <p className="text-[13px]">
                {app.resource.account_name || app.resource.account_id || 'account'}
                {app.resource.pages_project ? ` · ${app.resource.pages_project}` : ''}
                {app.resource.r2_bucket ? ` · ${app.resource.r2_bucket}` : ''}
              </p>
              {accounts.length > 0 && (
                <select
                  className="flat-input"
                  value={app.resource.account_id || ''}
                  onChange={(e) => {
                    const account = accounts.find((a) => a.id === e.target.value)
                    if (!account) return
                    act(async () => {
                      await api(`/api/projects/${ctx.slug}/apps/cloudflare/bind`, {
                        method: 'POST',
                        body: JSON.stringify({ account_id: account.id, account_name: account.name }),
                      })
                      await load()
                      await loadCf()
                    })
                  }}
                >
                  {accounts.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name}
                    </option>
                  ))}
                </select>
              )}
              {pages.length > 0 && (
                <select
                  className="flat-input"
                  value={app.resource.pages_project || ''}
                  onChange={(e) => {
                    act(async () => {
                      await api(`/api/projects/${ctx.slug}/apps/cloudflare/bind`, {
                        method: 'POST',
                        body: JSON.stringify({ pages_project: e.target.value }),
                      })
                      await load()
                    })
                  }}
                >
                  <option value="">pages project</option>
                  {pages.map((p) => (
                    <option key={p.name} value={p.name}>
                      {p.name}
                    </option>
                  ))}
                </select>
              )}
              {buckets.length > 0 && (
                <select
                  className="flat-input"
                  value={app.resource.r2_bucket || ''}
                  onChange={(e) => {
                    act(async () => {
                      await api(`/api/projects/${ctx.slug}/apps/cloudflare/bind`, {
                        method: 'POST',
                        body: JSON.stringify({ r2_bucket: e.target.value }),
                      })
                      await load()
                    })
                  }}
                >
                  <option value="">r2 bucket</option>
                  {buckets.map((b) => (
                    <option key={b.name} value={b.name}>
                      {b.name}
                    </option>
                  ))}
                </select>
              )}
              <div className="flex gap-2">
                <Action disabled={busy} onClick={() => act(loadCf)}>
                  {accounts.length ? 'refresh' : 'load account'}
                </Action>
                <Action
                  tone="danger"
                  disabled={busy}
                  onClick={() =>
                    act(async () => {
                      await api(`/api/projects/${ctx.slug}/apps/cloudflare/uninstall`, { method: 'POST' })
                      setAccounts([])
                      setPages([])
                      setBuckets([])
                      await load()
                    })
                  }
                >
                  disconnect
                </Action>
              </div>
            </>
          )}
        </Section>
      ))}
    </div>
  )
}

function SettingsPane({ ctx }: { ctx: InspectorCtx }) {
  const p = ctx.project
  const [name, setName] = useState(p?.name || '')
  const [ingest, setIngest] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [flash, setFlash] = useState<string | null>(null)

  useEffect(() => {
    setName(p?.name || '')
  }, [p?.id, p?.name])

  async function act(fn: () => Promise<void>) {
    setBusy(true)
    setFlash(null)
    try {
      await fn()
    } catch (e) {
      setFlash(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      {flash && <div className="px-4 py-2 text-[13px] text-red-400">{flash}</div>}
      <Section title="project">
        <Field label="name" value={name} onChange={(e) => setName(e.target.value)} />
        <Action
          tone="primary"
          disabled={busy || !name.trim()}
          onClick={() =>
            act(async () => {
              await api(`/api/projects/${ctx.slug}`, {
                method: 'PATCH',
                body: JSON.stringify({ name: name.trim() }),
              })
              ctx.onAssetsChanged()
            })
          }
        >
          save name
        </Action>
      </Section>
      <Section title="ingest" hint="One live token. Rotate invalidates the previous value.">
        <Action
          disabled={busy}
          onClick={() =>
            act(async () => {
              const r = await api<{ token: string }>(`/api/projects/${ctx.slug}/ingest-token`, {
                method: 'POST',
              })
              setIngest(r.token)
            })
          }
        >
          rotate token
        </Action>
        {ingest && (
          <pre className="whitespace-pre-wrap break-all font-mono text-[12px] text-accent">{ingest}</pre>
        )}
      </Section>
      <Section title="dispatch">
        <Action
          disabled={busy}
          onClick={() =>
            act(async () => {
              await api(`/api/projects/${ctx.slug}/dispatch`, {
                method: 'POST',
                body: JSON.stringify({ instance: 'dev' }),
              })
            })
          }
        >
          dispatch dev
        </Action>
      </Section>
      <Section title="health" hint={`${ctx.errors.length} recent`}>
        {ctx.errors.length === 0 && <p className="text-[13px] text-faint">clean</p>}
        {ctx.errors.slice(0, 12).map((e) => (
          <div key={e.id} className="border-b border-border py-2">
            <div className="text-[12px] tracking-[0.06em] text-accent">
              {e.source} · {e.level}
            </div>
            <div className="text-[13px]">{e.message}</div>
          </div>
        ))}
      </Section>
    </div>
  )
}

export function RepoTree({
  slug,
  selected,
  onOpen,
  tick,
}: {
  slug: string
  selected: string | null
  onOpen: (path: string, ref: string) => void
  tick: number
}) {
  const [branch, setBranch] = useState('')
  const ref = 'HEAD'
  const [paths, setPaths] = useState<{ path: string; size: number }[]>([])
  const [open, setOpen] = useState<Record<string, boolean>>({ '': true })
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    api<{ branch: string; ref: string; entries: { path: string; size: number }[] }>(
      `/api/projects/${slug}/tree?ref=${encodeURIComponent(ref)}`,
    )
      .then((t) => {
        setBranch(t.branch)
        setPaths(t.entries)
        setErr(null)
      })
      .catch((e) => setErr(String(e)))
  }, [slug, ref, tick])

  const nodes = useMemo(() => nest(paths.map((e) => e.path)), [paths])

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center justify-between px-3 py-2 text-[12px] tracking-[0.08em] text-faint">
        <span>{branch || '…'}</span>
        <span>{paths.length}</span>
      </div>
      {err && <div className="px-3 text-[12px] text-red-400">{err}</div>}
      <div className="min-h-0 flex-1 overflow-y-auto px-1 pb-2">
        {nodes.map((n) => (
          <TreeNode
            key={n.path}
            node={n}
            depth={0}
            open={open}
            setOpen={setOpen}
            selected={selected}
            onFile={(p) => onOpen(p, ref)}
          />
        ))}
        {nodes.length === 0 && !err && <p className="px-3 py-4 text-[13px] text-faint">empty tree</p>}
      </div>
    </div>
  )
}

type TNode = { name: string; path: string; type: 'dir' | 'file'; children: TNode[] }

function nest(paths: string[]): TNode[] {
  const root: TNode[] = []
  const index = new Map<string, TNode>()
  for (const path of paths) {
    const parts = path.split('/').filter(Boolean)
    let acc = ''
    let siblings = root
    parts.forEach((part, i) => {
      acc = acc ? `${acc}/${part}` : part
      let node = index.get(acc)
      if (!node) {
        node = { name: part, path: acc, type: i === parts.length - 1 ? 'file' : 'dir', children: [] }
        index.set(acc, node)
        siblings.push(node)
      }
      if (node.type === 'dir' && i === parts.length - 1) {
        node.type = 'file'
      }
      siblings = node.children
    })
  }
  const sort = (xs: TNode[]) => {
    xs.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'dir' ? -1 : 1
      return a.name.localeCompare(b.name)
    })
    xs.forEach((x) => sort(x.children))
  }
  sort(root)
  return root
}

function TreeNode({
  node,
  depth,
  open,
  setOpen,
  selected,
  onFile,
}: {
  node: TNode
  depth: number
  open: Record<string, boolean>
  setOpen: (fn: (o: Record<string, boolean>) => Record<string, boolean>) => void
  selected: string | null
  onFile: (path: string) => void
}) {
  const expanded = open[node.path] ?? depth < 1
  if (node.type === 'dir') {
    return (
      <div>
        <button
          type="button"
          className="flex w-full items-center gap-1 px-2 py-0.5 text-left text-[13px] text-muted hover:text-fg"
          style={{ paddingLeft: 8 + depth * 12 }}
          onClick={() => setOpen((o) => ({ ...o, [node.path]: !expanded }))}
        >
          <span className="w-3 text-faint">{expanded ? '–' : '+'}</span>
          {node.name}
        </button>
        {expanded &&
          node.children.map((c) => (
            <TreeNode
              key={c.path}
              node={c}
              depth={depth + 1}
              open={open}
              setOpen={setOpen}
              selected={selected}
              onFile={onFile}
            />
          ))}
      </div>
    )
  }
  return (
    <button
      type="button"
      className={cn(
        'block w-full truncate px-2 py-0.5 text-left font-mono text-[12px]',
        selected === node.path ? 'text-accent' : 'text-fg hover:text-accent',
      )}
      style={{ paddingLeft: 20 + depth * 12 }}
      onClick={() => onFile(node.path)}
    >
      {node.name}
    </button>
  )
}

function extKind(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() || ''
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(ext)) return 'image'
  if (['md', 'markdown'].includes(ext)) return 'markdown'
  if (ext === 'json') return 'json'
  return 'text'
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} b`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} kb`
  return `${(n / 1024 / 1024).toFixed(1)} mb`
}

function prettyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

function splitMd(text: string): { type: 'p' | 'h' | 'li' | 'code'; text: string }[] {
  const out: { type: 'p' | 'h' | 'li' | 'code'; text: string }[] = []
  const parts = text.split(/```/)
  parts.forEach((chunk, i) => {
    if (i % 2 === 1) {
      const nl = chunk.indexOf('\n')
      out.push({ type: 'code', text: nl >= 0 ? chunk.slice(nl + 1) : chunk })
      return
    }
    for (const line of chunk.split('\n')) {
      const t = line.trim()
      if (!t) continue
      if (t.startsWith('#')) out.push({ type: 'h', text: t.replace(/^#+\s*/, '') })
      else if (t.startsWith('- ') || t.startsWith('* ')) out.push({ type: 'li', text: t.slice(2) })
      else out.push({ type: 'p', text: t })
    }
  })
  return out
}

import { useCallback, useEffect, useState } from 'react'
import { Cloud, GitBranch, Plus, Trash2 } from 'lucide-react'
import { Button } from './components/ui/button'
import { api, type Project } from './lib/api'
import { cn } from './lib/utils'

export default function Projects({ onOpen }: { onOpen: (slug: string) => void }) {
  const [projects, setProjects] = useState<Project[]>([])
  const [flash, setFlash] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [git, setGit] = useState('')
  const [once, setOnce] = useState<{ slug: string; token: string } | null>(null)
  const [renameSlug, setRenameSlug] = useState<string | null>(null)
  const [renameName, setRenameName] = useState('')
  const [dropSlug, setDropSlug] = useState<string | null>(null)

  const load = useCallback(async () => {
    const rows = await api<Project[]>('/api/projects')
    setProjects(rows)
  }, [])

  useEffect(() => {
    load().catch((e) => setFlash(String(e)))
  }, [load])

  async function act(fn: () => Promise<void>) {
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

  return (
    <div className="flex h-full flex-col bg-bg text-fg">
      <header className="flex h-16 items-center justify-between border-b border-border bg-panel px-5">
        <div className="flex items-center gap-4">
          <span className="text-[20px] tracking-[0.16em] text-accent">FACTORY</span>
          <span className="text-muted">projects</span>
          <span className="text-faint">{projects.length}</span>
        </div>
        <Button size="sm" onClick={() => setCreating((v) => !v)}>
          <Plus size={16} /> new project
        </Button>
      </header>

      {flash && (
        <div className="border-b border-red-900 bg-red-950/40 px-4 py-2 text-red-300">{flash}</div>
      )}

      {once && (
        <div className="border-b border-accent/40 bg-accent/10 px-5 py-3 text-[16px]">
          <div className="text-[13px] tracking-[0.08em] text-accent">INGEST TOKEN · copy now</div>
          <pre className="mt-1 whitespace-pre-wrap break-all font-mono text-[14px]">{once.token}</pre>
          <div className="mt-1 text-faint">
            {once.slug} — shown once. Rotate later from project settings.
          </div>
          <Button className="mt-2" size="sm" variant="ghost" onClick={() => setOnce(null)}>
            dismissed
          </Button>
        </div>
      )}

      {creating && (
        <form
          className="grid gap-3 border-b border-border bg-panel px-5 py-4 md:grid-cols-[1fr_1fr_2fr_auto]"
          onSubmit={(e) => {
            e.preventDefault()
            if (!name.trim()) return
            act(async () => {
              const p = await api<Project>('/api/projects', {
                method: 'POST',
                body: JSON.stringify({
                  name: name.trim(),
                  slug: slug.trim() || undefined,
                  git_remote: git.trim() || undefined,
                }),
              })
              setName('')
              setSlug('')
              setGit('')
              setCreating(false)
              if (p.ingest_token) setOnce({ slug: p.slug, token: p.ingest_token })
            })
          }}
        >
          <input
            autoFocus
            className="h-11 border border-border bg-bg px-3 text-[16px] outline-none focus:border-accent"
            placeholder="name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <input
            className="h-11 border border-border bg-bg px-3 text-[16px] outline-none focus:border-accent"
            placeholder="slug (optional)"
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
          />
          <input
            className="h-11 border border-border bg-bg px-3 text-[16px] outline-none focus:border-accent"
            placeholder="git remote (https://github.com/…)"
            value={git}
            onChange={(e) => setGit(e.target.value)}
          />
          <div className="flex gap-2">
            <Button size="sm" disabled={busy || !name.trim()}>
              create
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setCreating(false)}>
              cancel
            </Button>
          </div>
        </form>
      )}

      <main className="min-h-0 flex-1 overflow-y-auto p-5">
        {projects.length === 0 && (
          <p className="text-faint">no projects yet — create one to open a board</p>
        )}
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {projects.map((p) => {
            const listed = p.connections?.apps
            const gh = listed?.find((a) => a.id === 'github')
            const cfApp = listed?.find((a) => a.id === 'cloudflare')
            const gitRemote = p.connections?.git || p.git_remote
            const repo = gh?.resource?.repo
            const cfLabel = cfApp?.resource?.pages_project || cfApp?.resource?.account_name
            return (
              <article
                key={p.id}
                className="flex flex-col border border-border bg-panel hover:border-accent/60"
              >
                <button
                  type="button"
                  className="flex-1 px-4 py-4 text-left"
                  onClick={() => onOpen(p.slug)}
                >
                  <div className="text-[13px] tracking-[0.08em] text-faint">{p.slug}</div>
                  <h2 className="mt-1 text-[22px] leading-tight">{p.name}</h2>
                  <div className="mt-3 flex flex-wrap gap-3 text-[14px] text-muted">
                    <span>{p.open_tickets ?? 0} open</span>
                    <span>{p.file_count ?? 0} files</span>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-3 text-[13px] text-faint">
                    <span className={cn('inline-flex items-center gap-1', (gh?.installed || gitRemote) && 'text-accent')}>
                      <GitBranch size={14} />
                      {repo || (gitRemote ? gitRemote.replace(/^https:\/\/github.com\//, '') : 'GitHub')}
                    </span>
                    <span className={cn('inline-flex items-center gap-1', cfApp?.installed && 'text-accent')}>
                      <Cloud size={14} />
                      {cfApp?.installed ? cfLabel || 'Cloudflare' : 'Cloudflare'}
                    </span>
                  </div>
                </button>
                <div className="flex items-center justify-end gap-1 border-t border-border px-2 py-2">
                  {renameSlug === p.slug ? (
                    <form
                      className="flex flex-1 gap-2"
                      onSubmit={(e) => {
                        e.preventDefault()
                        act(async () => {
                          await api(`/api/projects/${p.slug}`, {
                            method: 'PATCH',
                            body: JSON.stringify({ name: renameName.trim() }),
                          })
                          setRenameSlug(null)
                        })
                      }}
                    >
                      <input
                        autoFocus
                        className="h-9 flex-1 border border-border bg-bg px-2 text-[15px] outline-none focus:border-accent"
                        value={renameName}
                        onChange={(e) => setRenameName(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Escape') setRenameSlug(null)
                        }}
                      />
                      <Button size="sm" disabled={busy || !renameName.trim()}>
                        save
                      </Button>
                    </form>
                  ) : (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        setRenameSlug(p.slug)
                        setRenameName(p.name)
                      }}
                    >
                      rename
                    </Button>
                  )}
                  {dropSlug === p.slug ? (
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={busy}
                      onClick={() =>
                        act(async () => {
                          await api(`/api/projects/${p.slug}`, { method: 'DELETE' })
                          setDropSlug(null)
                        })
                      }
                    >
                      confirm delete
                    </Button>
                  ) : (
                    <Button size="sm" variant="ghost" onClick={() => setDropSlug(p.slug)}>
                      <Trash2 size={15} />
                    </Button>
                  )}
                </div>
              </article>
            )
          })}
        </div>
      </main>
    </div>
  )
}

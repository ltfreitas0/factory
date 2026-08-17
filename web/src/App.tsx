import { useEffect, useState } from 'react'
import Board from './Board'
import Projects from './Projects'
import Workspace from './Workspace'
import { api, AUTH_KEY } from './lib/api'

function parseHash(hash: string): { slug: string | null; board: boolean } {
  const rest = hash.replace(/^#/, '')
  const m = rest.match(/^\/p\/([a-z0-9][a-z0-9-]{0,62})(\/board)?\/?$/)
  return { slug: m ? m[1] : null, board: Boolean(m && m[2]) }
}

function Gate({ onOk }: { onOk: () => void }) {
  const [token, setToken] = useState('')
  const [err, setErr] = useState<string | null>(null)
  return (
    <div className="flex h-full items-center justify-center bg-bg text-fg">
      <form
        className="w-[min(420px,90vw)] border border-border bg-panel p-6"
        onSubmit={async (e) => {
          e.preventDefault()
          setErr(null)
          try {
            await api('/auth/login', { method: 'POST', body: JSON.stringify({ token }) })
            localStorage.setItem(AUTH_KEY, token)
            onOk()
          } catch {
            setErr('rejected')
          }
        }}
      >
        <div className="text-[16px] tracking-[0.16em] text-accent">FACTORY</div>
        <p className="mt-2 text-[14px] text-faint">sign in with google</p>
        <button
          type="button"
          onClick={() => {
            window.location.href = '/auth/google?next=' + encodeURIComponent(window.location.hash)
          }}
          className="mt-4 h-10 w-full border border-border text-[14px] text-fg hover:border-accent/60 hover:text-accent"
        >
          continue with google
        </button>
        <div className="mt-6 flex items-center gap-3">
          <div className="h-px flex-1 bg-border" />
          <span className="text-[11px] text-faint">or board token</span>
          <div className="h-px flex-1 bg-border" />
        </div>
        <input
          autoFocus
          type="password"
          className="mt-4 h-10 w-full border-0 border-b border-border bg-transparent text-[15px] outline-none focus:border-accent"
          placeholder="token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
        {err && <p className="mt-2 text-[13px] text-red-400">{err}</p>}
        <button type="submit" className="mt-4 text-[13px] tracking-[0.1em] text-accent uppercase">
          enter
        </button>
      </form>
    </div>
  )
}

export default function App() {
  const [route, setRoute] = useState(() => parseHash(window.location.hash))
  const [authed, setAuthed] = useState(true)

  useEffect(() => {
    const onHash = () => setRoute(parseHash(window.location.hash))
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    api('/api/projects')
      .then(() => setAuthed(true))
      .catch((e: { status?: number }) => {
        if (e.status === 401) setAuthed(false)
      })
  }, [])

  // Google OAuth callback: /#/auth?token=...
  useEffect(() => {
    const m = window.location.hash.match(/^#\/auth\?token=([^&]+)/)
    if (!m) return
    localStorage.setItem(AUTH_KEY, decodeURIComponent(m[1]))
    window.history.replaceState({}, '', window.location.pathname + '#/')
    setRoute({ slug: null, board: false })
    setAuthed(true)
  }, [])

  useEffect(() => {
    const q = new URLSearchParams(window.location.search)
    const code = q.get('code')
    const state = q.get('state')
    if (!code || !state) return
    api<{ slug?: string }>('/api/apps/oauth/callback', {
      method: 'POST',
      body: JSON.stringify({ code, state }),
    })
      .then((r) => {
        sessionStorage.setItem('factory-tab', 'apps')
        const next = r.slug ? `#/p/${r.slug}` : '#/'
        window.history.replaceState({}, '', window.location.pathname + next)
        setRoute(parseHash(next))
      })
      .catch(() => {
        window.history.replaceState({}, '', window.location.pathname + window.location.hash)
      })
  }, [])

  if (!authed) {
    return <Gate onOk={() => setAuthed(true)} />
  }

  if (!route.slug) {
    return (
      <Projects
        onOpen={(s) => {
          window.location.hash = `#/p/${s}`
        }}
      />
    )
  }

  if (route.board) {
    return (
      <Board
        slug={route.slug}
        onBack={() => {
          window.location.hash = '#/'
        }}
      />
    )
  }

  return (
    <Workspace
      slug={route.slug}
      onBack={() => {
        window.location.hash = '#/'
      }}
      onBoard={() => {
        window.location.hash = `#/p/${route.slug}/board`
      }}
    />
  )
}

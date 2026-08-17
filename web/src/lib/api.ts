export const AUTH_KEY = 'factory-token'

export function apiBase(): string {
  const raw = (import.meta as { env?: { VITE_FACTORY_API?: string } }).env?.VITE_FACTORY_API
  return (raw || '').replace(/\/$/, '')
}

export function authToken(): string | null {
  return localStorage.getItem(AUTH_KEY)
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const token = authToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((init?.headers as Record<string, string>) || {}),
  }
  if (token) headers.Authorization = `Bearer ${token}`
  const res = await fetch(`${apiBase()}${path}`, { ...init, headers })
  if (!res.ok) {
    const t = await res.text()
    const err = new Error(t || res.statusText) as Error & { status?: number }
    err.status = res.status
    throw err
  }
  if (res.status === 204) return undefined as T
  const text = await res.text()
  if (!text) return undefined as T
  return JSON.parse(text) as T
}

export type Stage = {
  id: string
  title?: string
  kind: string
  file?: string
  plugin?: string
  muted?: boolean
}

export type CloudflareConn = {
  set?: boolean
  token_set?: boolean
  account_id?: string
  pages_project?: string
  r2_bucket?: string
}

export type AppResource = {
  repo?: string
  branch?: string
  account_id?: string
  account_name?: string
  pages_project?: string
  r2_bucket?: string
}

export type ProjectApp = {
  id: string
  title: string
  summary: string
  installed: boolean
  oauth_available: boolean
  env_ready: boolean
  identity: string
  resource: AppResource
}

export type Project = {
  id: string
  slug: string
  name: string
  repo_path?: string
  validate_cmd?: string
  git_remote?: string | null
  workflow: Stage[]
  mode?: 'live' | 'tickets'
  quality_gates?: number
  owner_id?: string | null
  open_tickets?: number
  file_count?: number
  connections?: {
    git?: string | null
    cloudflare?: CloudflareConn
    apps?: ProjectApp[]
  }
  ingest_token?: string
}

export type FileEntry = {
  store: string
  path: string
  set: boolean
  updated_at?: string | null
  body?: string
  bytes?: number
  kind?: string
  mime?: string
}

export type TreeEntry = { path: string; type: string; size: number }

export type TreeListing = {
  branch: string
  ref: string
  entries: TreeEntry[]
}

export type TreeFile = {
  path: string
  ref: string
  kind: 'text' | 'markdown' | 'json' | 'image' | 'binary' | string
  mime: string
  bytes: number
  binary: boolean
  body: string | null
}

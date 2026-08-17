import { ChevronDown, ChevronRight } from 'lucide-react'
import { useState, type ReactNode } from 'react'

export function CollapseBtn({
  open,
  onToggle,
  label,
}: {
  open: boolean
  onToggle: () => void
  label: string
}) {
  const Icon = open ? ChevronDown : ChevronRight
  return (
    <button
      type="button"
      className="inline-flex h-8 w-8 shrink-0 items-center justify-center text-faint hover:text-accent"
      title={open ? `collapse ${label}` : `expand ${label}`}
      aria-expanded={open}
      aria-label={open ? `collapse ${label}` : `expand ${label}`}
      onClick={onToggle}
    >
      <Icon size={16} strokeWidth={1.75} />
    </button>
  )
}

export function CollapsedStrip({
  title,
  onToggle,
  extra,
}: {
  title: string
  onToggle: () => void
  extra?: ReactNode
}) {
  return (
    <aside className="flex w-9 shrink-0 flex-col items-center border-l border-border bg-panel">
      <CollapseBtn open={false} onToggle={onToggle} label={title} />
      {extra}
      <button
        type="button"
        className="mt-2 flex flex-1 items-start justify-center"
        onClick={onToggle}
        title={`expand ${title}`}
      >
        <span className="[writing-mode:vertical-rl] rotate-180 text-[11px] tracking-[0.16em] text-faint uppercase">
          {title}
        </span>
      </button>
    </aside>
  )
}

export function ColumnHead({
  title,
  open,
  onToggle,
  extra,
  children,
}: {
  title: string
  open: boolean
  onToggle: () => void
  extra?: ReactNode
  children?: ReactNode
}) {
  return (
    <header className="flex h-9 shrink-0 items-center gap-1 border-b border-border px-1">
      <CollapseBtn open={open} onToggle={onToggle} label={title} />
      {children ?? (
        <span className="flex-1 truncate text-[11px] tracking-[0.14em] text-faint uppercase">
          {title}
        </span>
      )}
      {extra}
    </header>
  )
}

export function useColumnOpen(key: string, fallback = true): [boolean, () => void] {
  const storageKey = `factory-col:${key}`
  const [open, setOpen] = useState<boolean>(() => {
    try {
      const raw = localStorage.getItem(storageKey)
      if (raw === '0') return false
      if (raw === '1') return true
    } catch {
      /* ignore */
    }
    return fallback
  })
  return [
    open,
    () => {
      setOpen((v) => {
        const next = !v
        try {
          localStorage.setItem(storageKey, next ? '1' : '0')
        } catch {
          /* ignore */
        }
        return next
      })
    },
  ]
}

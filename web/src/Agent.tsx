import { useEffect, useRef, useState } from 'react'
import { ArrowUp } from 'lucide-react'
import { CollapsedStrip, ColumnHead } from './Rail'

type Line = { id: string; role: 'user' | 'system'; text: string }

export default function Agent({
  projectName,
  open,
  onToggle,
}: {
  projectName: string
  open: boolean
  onToggle: () => void
}) {
  const [draft, setDraft] = useState('')
  const [lines, setLines] = useState<Line[]>([])
  const end = useRef<HTMLDivElement>(null)

  useEffect(() => {
    end.current?.scrollIntoView({ block: 'end' })
  }, [lines])

  if (!open) {
    return <CollapsedStrip title="agent" onToggle={onToggle} />
  }

  function send() {
    const text = draft.trim()
    if (!text) return
    setDraft('')
    setLines((prev) => [
      ...prev,
      { id: `u-${Date.now()}`, role: 'user', text },
      {
        id: `s-${Date.now()}`,
        role: 'system',
        text: 'Harness is not connected yet. This column is the seat for the master agent.',
      },
    ])
  }

  return (
    <aside className="flex w-[22vw] max-w-[360px] min-w-[240px] shrink-0 flex-col border-l border-border bg-panel">
      <ColumnHead title="agent" open={open} onToggle={onToggle} extra={
        <span className="truncate pr-2 text-[11px] text-faint">{projectName}</span>
      } />

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        {lines.length === 0 && (
          <p className="text-[13px] leading-6 text-faint">
            Master agent. Issue, steer, and orchestrate tickets from here. The harness comes later —
            the seat is ready.
          </p>
        )}
        {lines.map((line) => (
          <div
            key={line.id}
            className={
              line.role === 'user'
                ? 'mb-3 text-[14px] leading-6 text-fg'
                : 'mb-3 text-[13px] leading-6 text-faint'
            }
          >
            <div className="mb-0.5 text-[10px] tracking-[0.12em] uppercase text-faint">
              {line.role === 'user' ? 'you' : 'agent'}
            </div>
            {line.text}
          </div>
        ))}
        <div ref={end} />
      </div>

      <form
        className="flex shrink-0 items-end gap-2 border-t border-border px-2 py-2"
        onSubmit={(e) => {
          e.preventDefault()
          send()
        }}
      >
        <textarea
          className="max-h-32 min-h-[36px] flex-1 resize-none border-0 bg-transparent py-2 text-[14px] leading-5 outline-none placeholder:text-faint"
          rows={1}
          placeholder="direct the factory…"
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
          disabled={!draft.trim()}
          className="mb-1 inline-flex h-8 w-8 items-center justify-center text-accent disabled:text-faint"
          title="send"
        >
          <ArrowUp size={16} />
        </button>
      </form>
    </aside>
  )
}

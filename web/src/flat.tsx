import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, TextareaHTMLAttributes } from 'react'
import { cn } from './lib/utils'

export function Section({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <section className="inspector-section">
      <header className="mb-3">
        <h3>{title}</h3>
        {hint && <p className="mt-1 text-[13px] leading-snug text-faint">{hint}</p>}
      </header>
      <div className="flex flex-col gap-2.5">{children}</div>
    </section>
  )
}

export function Field({
  label,
  className,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { label?: string }) {
  return (
    <label className="block">
      {label && <span className="mb-1 block text-[12px] tracking-[0.08em] text-faint">{label}</span>}
      <input className={cn('flat-input', className)} {...props} />
    </label>
  )
}

export function Area({
  label,
  className,
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement> & { label?: string }) {
  return (
    <label className="block">
      {label && <span className="mb-1 block text-[12px] tracking-[0.08em] text-faint">{label}</span>}
      <textarea className={cn('flat-area', className)} {...props} />
    </label>
  )
}

export function Action({
  tone = 'default',
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { tone?: 'default' | 'primary' | 'danger' }) {
  return (
    <button
      type={props.type ?? 'button'}
      className={cn(
        'flat-btn',
        tone === 'primary' && 'flat-btn-primary',
        tone === 'danger' && 'flat-btn-danger',
        className,
      )}
      {...props}
    />
  )
}

export function Tabs({
  items,
  value,
  onChange,
}: {
  items: { id: string; label: string }[]
  value: string
  onChange: (id: string) => void
}) {
  return (
    <nav className="inspector-tabs">
      {items.map((t) => (
        <button
          key={t.id}
          type="button"
          className={cn('inspector-tab', value === t.id && 'is-on')}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </nav>
  )
}

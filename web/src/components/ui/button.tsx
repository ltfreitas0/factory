import { cva, type VariantProps } from 'class-variance-authority'
import type { ButtonHTMLAttributes } from 'react'
import { cn } from '../../lib/utils'

const buttonVariants = cva(
  'inline-flex items-center justify-center gap-1 rounded-sm text-[13px] tracking-wide transition-colors disabled:opacity-40',
  {
    variants: {
      variant: {
        default: 'bg-accent/15 text-accent border border-accent hover:bg-accent/25',
        ghost: 'text-muted hover:text-fg hover:bg-panel2 border border-transparent',
        danger: 'text-red-400 border border-red-900 hover:bg-red-950/40',
      },
      size: {
        default: 'h-8 px-3',
        sm: 'h-7 px-2 text-[12px]',
      },
    },
    defaultVariants: { variant: 'default', size: 'default' },
  },
)

export function Button({
  className,
  variant,
  size,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof buttonVariants>) {
  return <button className={cn(buttonVariants({ variant, size }), className)} {...props} />
}

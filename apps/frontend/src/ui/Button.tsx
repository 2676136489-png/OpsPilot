import clsx from 'clsx'
import type { ButtonHTMLAttributes, ReactNode } from 'react'

export type ButtonVariant =
  | 'primary'
  | 'agent'
  | 'default'
  | 'ghost'
  | 'danger'
  | 'danger-ghost'
  | 'success'

export type ButtonSize = 'sm' | 'md' | 'lg'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
  block?: boolean
  icon?: ReactNode
  iconRight?: ReactNode
}

export function Button({
  variant = 'default',
  size = 'md',
  loading = false,
  block = false,
  icon,
  iconRight,
  className,
  children,
  disabled,
  ...rest
}: ButtonProps) {
  return (
    <button
      type="button"
      className={clsx(
        'btn',
        `btn-${variant}`,
        size !== 'md' && `btn-${size}`,
        block && 'btn-block',
        !children && 'btn-icon',
        className,
      )}
      disabled={disabled || loading}
      {...rest}
    >
      {loading ? <span className="btn-spinner" aria-hidden /> : icon}
      {children}
      {!loading && iconRight}
    </button>
  )
}

export interface IconButtonProps extends Omit<ButtonProps, 'icon' | 'iconRight' | 'block'> {
  label: string
  children: ReactNode
}

export function IconButton({ label, className, children, ...rest }: IconButtonProps) {
  return (
    <Button {...rest} className={clsx('btn-icon', className)} aria-label={label} title={label}>
      {children}
    </Button>
  )
}

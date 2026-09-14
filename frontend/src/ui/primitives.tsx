import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from 'react'
import { AlertCircle, CheckCircle2, Info, TriangleAlert } from 'lucide-react'
import { presentStatus, type StatusTone } from './status'

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'

export function Button({ variant = 'secondary', className = '', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  return <button className={`ds-button ds-button--${variant} ${className}`.trim()} {...props} />
}

export function StatusBadge({ status, label, className = '' }: { status: string | null | undefined; label?: string; className?: string }) {
  const presentation = presentStatus(status)
  return <span className={`ds-status ds-status--${presentation.tone} ${className}`.trim()} role="status">
    <span className="ds-status__dot" aria-hidden="true" />
    {label ?? presentation.label}
  </span>
}

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description?: string; actions?: ReactNode }) {
  return <header className="ds-page-header">
    <div className="ds-page-header__copy">
      {eyebrow ? <p className="ds-eyebrow">{eyebrow}</p> : null}
      <h1>{title}</h1>
      {description ? <p>{description}</p> : null}
    </div>
    {actions ? <div className="ds-page-header__actions">{actions}</div> : null}
  </header>
}

export function Surface({ children, className = '', ...props }: HTMLAttributes<HTMLElement> & { children: ReactNode }) {
  return <section className={`ds-surface ${className}`.trim()} {...props}>{children}</section>
}

export function EmptyState({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return <div className="ds-empty-state">
    <div className="ds-empty-state__icon" aria-hidden="true">◇</div>
    <h2>{title}</h2>
    {description ? <p>{description}</p> : null}
    {action ? <div className="ds-empty-state__action">{action}</div> : null}
  </div>
}

const alertIcons = {
  neutral: Info,
  info: Info,
  success: CheckCircle2,
  warning: TriangleAlert,
  danger: AlertCircle,
} satisfies Record<StatusTone, typeof Info>

export function Alert({ tone = 'info', title, children }: { tone?: StatusTone; title: string; children?: ReactNode }) {
  const Icon = alertIcons[tone]
  return <div className={`ds-alert ds-alert--${tone}`} role={tone === 'danger' ? 'alert' : 'status'}>
    <Icon size={18} aria-hidden="true" />
    <div><strong>{title}</strong>{children ? <div>{children}</div> : null}</div>
  </div>
}

export function MetricCard({ label, value, note, icon }: { label: string; value: string | number; note?: string; icon?: ReactNode }) {
  return <article className="ds-metric-card">
    <div className="ds-metric-card__top">{icon}<span>{label}</span></div>
    <strong>{value}</strong>
    {note ? <small>{note}</small> : null}
  </article>
}

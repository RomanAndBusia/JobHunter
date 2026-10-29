import type { ReactNode } from 'react'

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle: string; actions?: ReactNode }) {
  return <div className="mb-7 flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
    <div><h1 className="page-title">{title}</h1><p className="page-kicker">{subtitle}</p></div>
    {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
  </div>
}

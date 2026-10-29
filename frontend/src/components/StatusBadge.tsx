const tones: Record<string, string> = {
  ACCEPTED: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  SUBMITTED: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  REVIEW_REQUIRED: 'bg-amber-50 text-amber-700 ring-amber-200',
  NEEDS_USER: 'bg-amber-50 text-amber-700 ring-amber-200',
  CAPTCHA: 'bg-orange-50 text-orange-700 ring-orange-200',
  PAUSED: 'bg-sky-50 text-sky-700 ring-sky-200',
  FAILED: 'bg-red-50 text-red-700 ring-red-200',
  REJECTED: 'bg-slate-100 text-slate-600 ring-slate-200',
  PENDING: 'bg-blue-50 text-blue-700 ring-blue-200',
}

export function StatusBadge({ status }: { status: string }) {
  return <span className={`inline-flex rounded-full px-2.5 py-1 text-[11px] font-bold tracking-wide ring-1 ring-inset ${tones[status] ?? 'bg-violet-50 text-violet-700 ring-violet-200'}`}>
    {status.replaceAll('_', ' ')}
  </span>
}

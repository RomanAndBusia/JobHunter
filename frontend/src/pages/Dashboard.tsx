import { useQuery } from '@tanstack/react-query'
import { Activity, ArrowRight, Bot, CheckCircle2, CircleAlert, Radar, Send, Sparkles } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { PageHeader } from '../components/PageHeader'

type Metrics = Record<string, number | object | null>

const metricConfig = [
  ['jobs_discovered', 'Jobs discovered', Radar, 'text-blue-600 bg-blue-50'],
  ['new_jobs', 'Awaiting evaluation', Sparkles, 'text-violet-600 bg-violet-50'],
  ['ai_accepted', 'Relevant matches', Bot, 'text-emerald-600 bg-emerald-50'],
  ['review_required', 'Ready for review', CheckCircle2, 'text-amber-600 bg-amber-50'],
  ['submitted', 'Submitted', Send, 'text-teal-600 bg-teal-50'],
  ['needs_user', 'Needs your input', CircleAlert, 'text-orange-600 bg-orange-50'],
] as const

export function Dashboard() {
  const { data, isLoading } = useQuery({ queryKey: ['dashboard'], queryFn: () => api<Metrics>('/dashboard'), refetchInterval: 30_000 })
  return <>
    <PageHeader title="Dashboard" subtitle="A quiet overview of discovery, matching, and applications." actions={<Link className="btn-primary" to="/jobs">Review jobs <ArrowRight size={16} /></Link>} />
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {metricConfig.map(([key, label, Icon, tone]) => <div className="card" key={key}>
        <div className="flex items-start justify-between"><div><p className="text-sm font-medium text-slate-500">{label}</p><p className="mt-3 text-3xl font-semibold tracking-tight">{isLoading ? '—' : String(data?.[key] ?? 0)}</p></div><div className={`rounded-xl p-2.5 ${tone}`}><Icon size={20} /></div></div>
      </div>)}
    </div>
    <div className="mt-6 grid gap-6 lg:grid-cols-[1.4fr_1fr]">
      <section className="card">
        <div className="mb-5 flex items-center gap-2"><Activity size={18} /><h2 className="font-semibold">Operating model</h2></div>
        <div className="grid gap-3 sm:grid-cols-3">
          {[['01', 'Discover', 'Search Just Join IT or load your authenticated recommendations.'], ['02', 'Qualify', 'Each vacancy gets an independent absolute score against your CV.'], ['03', 'Apply safely', 'Visible browser, verified fields, explicit final approval.']].map(([n, title, copy]) => <div className="rounded-xl border border-slate-200 p-4" key={n}><div className="text-xs font-bold text-accent">{n}</div><div className="mt-2 text-sm font-semibold">{title}</div><p className="mt-1 text-xs leading-relaxed text-slate-500">{copy}</p></div>)}
        </div>
      </section>
      <section className="card">
        <h2 className="font-semibold">Latest scan</h2>
        {data?.last_scan ? <pre className="mt-4 overflow-auto rounded-xl bg-slate-950 p-4 text-xs text-slate-200">{JSON.stringify(data.last_scan, null, 2)}</pre> : <p className="mt-4 text-sm text-slate-500">No search has run yet. Start from Search or Recommendations.</p>}
      </section>
    </div>
  </>
}

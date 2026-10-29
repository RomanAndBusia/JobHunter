import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { StatusBadge } from '../components/StatusBadge'
import type { Application } from '../types'

export function History() {
  const query = useQuery({ queryKey: ['applications', 'history'], queryFn: () => api<Application[]>('/applications') })
  const history = query.data?.filter((item) => ['SUBMITTED', 'FAILED', 'ABORTED', 'EXPIRED'].includes(item.status)) ?? []
  return <><PageHeader title="Application history" subtitle="A durable record of targets, answers, document provenance, and outcomes." /><div className="card overflow-hidden !p-0"><div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500"><tr><th className="px-5 py-3">Role</th><th className="px-5 py-3">Target</th><th className="px-5 py-3">Status</th><th className="px-5 py-3">Submitted</th><th className="px-5 py-3">Evidence</th></tr></thead><tbody>{history.map((item) => <tr className="border-b border-slate-100 last:border-0" key={item.id}><td className="px-5 py-4"><div className="font-semibold">{item.job.title}</div><div className="text-xs text-slate-500">{item.job.company}</div>{item.error && <div className="mt-1 max-w-sm text-xs text-red-600">{item.error}</div>}</td><td className="px-5 py-4 text-slate-600"><div>{item.target_host ?? '—'}</div><div className="mt-1 text-xs text-slate-400">{item.job.source}{item.job.provider ? ` · ${item.job.provider}` : ''}</div></td><td className="px-5 py-4"><StatusBadge status={item.status} /></td><td className="px-5 py-4 text-slate-500">{item.submitted_at ? new Date(item.submitted_at).toLocaleString() : '—'}</td><td className="px-5 py-4 text-slate-500"><div>{item.answers.length} answers · {item.document_versions.length} files</div><div className="mt-1 text-xs text-slate-400">AI: {String(item.review_data.ai_runtime ?? '—')}</div></td></tr>)}</tbody></table></div>{!history.length && <p className="p-10 text-center text-sm text-slate-500">No completed applications yet.</p>}</div></>
}

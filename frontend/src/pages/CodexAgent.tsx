import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, Play } from 'lucide-react'
import { api, post } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { StatusBadge } from '../components/StatusBadge'
import type { CodexRun } from '../types'

export function CodexAgent() {
  const client = useQueryClient()
  const [task, setTask] = useState('Find suitable new entry-level IT jobs for me and prepare applications.')
  const runs = useQuery({ queryKey: ['codex-runs'], queryFn: () => api<CodexRun[]>('/codex-runs'), refetchInterval: 3000 })
  const run = useMutation({ mutationFn: () => post('/codex-runs', { task }), onSuccess: () => void client.invalidateQueries({ queryKey: ['codex-runs'] }) })
  return <><PageHeader title="Codex agent" subtitle="High-level reasoning over safe JobHunter commands. Browser control and final submission remain deterministic." /><section className="card"><div className="flex items-center gap-3"><div className="grid h-11 w-11 place-items-center rounded-xl bg-violet-50 text-violet-700"><Bot /></div><div><h2 className="font-semibold">Operational task</h2><p className="text-sm text-slate-500">Codex may discover, classify, and prepare—but never submit.</p></div></div><textarea className="field mt-5 min-h-28" value={task} onChange={(event) => setTask(event.target.value)} /><button className="btn-primary mt-3" disabled={run.isPending || task.trim().length < 3} onClick={() => run.mutate()}><Play size={15} /> Run Codex</button>{run.error && <p className="mt-3 text-sm text-red-600">{run.error.message}</p>}</section><div className="mt-6 space-y-4">{runs.data?.map((item) => <article className="card" key={item.id}><div className="flex items-start justify-between gap-4"><div><p className="text-xs text-slate-500">Run #{item.id}</p><h3 className="mt-1 font-semibold">{item.task}</h3></div><StatusBadge status={item.status} /></div><div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">{['jobs_discovered', 'jobs_accepted', 'applications_prepared', 'questions_needing_user'].map((key) => <div className="rounded-xl bg-slate-50 p-3" key={key}><div className="text-[10px] uppercase tracking-wide text-slate-500">{key.replaceAll('_', ' ')}</div><div className="mt-1 text-xl font-semibold">{String(item.progress[key] ?? 0)}</div></div>)}</div>{item.error && <p className="mt-3 rounded-lg bg-red-50 p-3 text-sm text-red-700">{item.error}</p>}<details className="mt-4"><summary className="cursor-pointer text-xs font-semibold text-slate-500">Diagnostics</summary><pre className="mt-2 overflow-auto rounded-xl bg-slate-950 p-4 text-xs text-slate-200">{JSON.stringify({ progress: item.progress, result: item.result }, null, 2)}</pre></details></article>)}</div></>
}

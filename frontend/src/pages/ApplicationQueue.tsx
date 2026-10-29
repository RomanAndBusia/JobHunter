import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ExternalLink, Hand, Pause, Play, RefreshCw, RotateCcw, Square } from 'lucide-react'
import { Link, useLocation } from 'react-router-dom'
import { api, del, post } from '../api/client'
import { StartApplicationDialog } from '../components/StartApplicationDialog'
import { ConfirmAction } from '../components/ConfirmAction'
import { PageHeader } from '../components/PageHeader'
import { StatusBadge } from '../components/StatusBadge'
import type { Application } from '../types'
import { ApplicationQuestions } from '../components/ApplicationQuestions'
import { ApplicationLog } from '../components/ApplicationLog'

export function ApplicationQueue() {
  const client = useQueryClient()
  const location = useLocation()
  const [pendingStart, setPendingStart] = useState<Application>()
  const [globalNotice, setGlobalNotice] = useState((location.state as { notice?: string } | null)?.notice ?? '')
  const [notice, setNotice] = useState<Record<number, string>>({})
  const [actionError, setActionError] = useState<Record<number, string>>({})
  const query = useQuery({ queryKey: ['applications'], queryFn: () => api<Application[]>('/applications'), refetchInterval: 10_000 })
  const action = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) => post(`/applications/${id}/${name}`),
    onMutate: ({ id, name }) => {
      setActionError((value) => ({ ...value, [id]: '' })); setNotice((value) => ({ ...value, [id]: '' }))
      if (name === 'pause' || name === 'resume') client.setQueryData<Application[]>(['applications'], (items) => items?.map((item) => item.id === id ? { ...item, status: name === 'pause' ? 'PAUSED' : 'FORM_DISCOVERY', current_operation: name === 'pause' ? 'Pausing…' : 'Resuming…' } : item))
    },
    onSuccess: (_result, variables) => { setNotice((value) => ({ ...value, [variables.id]: `${variables.name.replace('-', ' ')} completed.` })); void client.invalidateQueries({ queryKey: ['applications'] }); void client.invalidateQueries({ queryKey: ['direct-application'] }); void client.invalidateQueries({ queryKey: ['jobs'] }) },
    onError: (error: Error, variables) => { setActionError((value) => ({ ...value, [variables.id]: error.message })); void client.invalidateQueries({ queryKey: ['applications'] }); void client.invalidateQueries({ queryKey: ['direct-application'] }); void client.invalidateQueries({ queryKey: ['jobs'] }) },
  })
  const remove = useMutation({
    mutationFn: (id: number) => del(`/applications/${id}`),
    onSuccess: (_result, id) => {
      client.setQueryData<Application[]>(['applications'], (items) => items?.filter((item) => item.id !== id))
      setGlobalNotice('Application and its vacancy-specific runtime data were deleted.')
      void client.invalidateQueries({ queryKey: ['applications'] }); void client.invalidateQueries({ queryKey: ['direct-application'] }); void client.invalidateQueries({ queryKey: ['jobs'] })
    },
    onError: (error: Error, id) => setActionError((value) => ({ ...value, [id]: error.message })),
  })
  const active = query.data?.filter((item) => item.status !== 'SUBMITTED') ?? []
  return <>
    {pendingStart && <StartApplicationDialog application={pendingStart} onClose={() => setPendingStart(undefined)} onStarted={() => setPendingStart(undefined)} />}
    <PageHeader title="Application queue" subtitle="Live browser work, manual interventions, and final-review gates." />
    {globalNotice && <div className="mb-4 flex items-center justify-between rounded-xl bg-emerald-50 p-3 text-sm text-emerald-700">{globalNotice}<button onClick={() => setGlobalNotice('')}>Dismiss</button></div>}
    {query.error && <p className="mb-4 rounded-xl bg-red-50 p-3 text-sm text-red-700">{query.error.message}</p>}
    <div className="grid gap-4 xl:grid-cols-2">
      {active.map((item) => <article className="card" key={item.id}>
        <div className="flex items-start justify-between gap-3"><div><p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{item.job.company}</p><h2 className="mt-1 text-lg font-semibold">{item.job.title}</h2><p className="mt-1 text-sm text-slate-500">{item.job.location}</p></div><StatusBadge status={item.status} /></div>
        <dl className="mt-5 grid grid-cols-2 gap-3 rounded-xl bg-slate-50 p-4 text-sm"><div><dt className="text-xs text-slate-500">Current website</dt><dd className="mt-1 truncate font-medium">{item.target_host ?? 'No live page reference — open Log for the reason'}</dd></div><div><dt className="text-xs text-slate-500">Step / fallback</dt><dd className="mt-1 font-medium">{item.current_step ?? '—'} / L{item.fallback_level}</dd></div><div className="col-span-2"><dt className="text-xs text-slate-500">Current operation</dt><dd className="mt-1 font-medium">{item.current_operation ?? 'Waiting'}</dd></div></dl>
        <p className="mt-3 text-sm font-medium">Suitability: {item.suitability_state ?? 'NOT_STARTED'}</p>
        {item.error && <p className="mt-3 rounded-lg bg-amber-50 p-3 text-xs leading-relaxed text-amber-800">{item.error}</p>}
        <ApplicationQuestions applicationId={item.id} />
        {notice[item.id] && <p className="mt-3 rounded-lg bg-emerald-50 p-3 text-xs text-emerald-700">{notice[item.id]}</p>}
        {actionError[item.id] && <p className="mt-3 rounded-lg bg-red-50 p-3 text-xs text-red-700">{actionError[item.id]}</p>}
        <div className="mt-4 flex flex-wrap gap-2">
          <ApplicationLog applicationId={item.id} />
          {item.allowed_actions.includes('pause') && <button className="btn-secondary" disabled={action.isPending} onClick={() => action.mutate({ id: item.id, name: 'pause' })}><Pause size={15} /> {action.isPending && action.variables?.id === item.id && action.variables.name === 'pause' ? 'Pausing…' : 'Pause'}</button>}
          {item.allowed_actions.includes('resume') && <button className="btn-secondary" disabled={action.isPending} onClick={() => action.mutate({ id: item.id, name: 'resume' })}><RotateCcw size={15} /> Resume</button>}
          {item.allowed_actions.includes('retry') && <button className="btn-secondary" disabled={action.isPending} onClick={() => action.mutate({ id: item.id, name: 'retry' })}><RefreshCw size={15} /> Retry step</button>}
          {item.allowed_actions.includes('manual') && <button className="btn-secondary" disabled={action.isPending} onClick={() => action.mutate({ id: item.id, name: 'manual' })}><Hand size={15} /> Manual help</button>}
          {item.allowed_actions.includes('open-browser') && <button className="btn-secondary" disabled={action.isPending} onClick={() => action.mutate({ id: item.id, name: 'open-browser' })}><ExternalLink size={15} /> Open browser</button>}
          {(item.allowed_actions.includes('select_cv') || item.allowed_actions.includes('start')) && <button className="btn-primary" onClick={() => setPendingStart(item)}><Play size={15} /> {item.allowed_actions.includes('start') ? 'Review CV & start' : 'Select CV'}</button>}
          {item.status === 'REVIEW_REQUIRED' && <Link className="btn-primary" to={`/applications/${item.id}`}>Review</Link>}
          {item.allowed_actions.includes('abort') && <button className="btn-danger" disabled={action.isPending} onClick={() => action.mutate({ id: item.id, name: 'abort' })}><Square size={14} /> Abort</button>}
          {item.allowed_actions.includes('reset') && <ConfirmAction label="RESET APPLICATION" message="Keep this application and selected CV, and clear its workflow data for another run?" disabled={action.isPending || remove.isPending} onConfirm={() => action.mutate({ id: item.id, name: 'reset' })} />}
          {item.allowed_actions.includes('delete') && <ConfirmAction label="DELETE APPLICATION" message="Permanently delete this application and its vacancy-specific runtime data?" danger disabled={action.isPending || remove.isPending} onConfirm={() => remove.mutate(item.id)} />}
        </div>
      </article>)}
      {!query.isLoading && !active.length && <div className="card col-span-full py-14 text-center text-sm text-slate-500">The queue is clear.</div>}
    </div>
  </>
}

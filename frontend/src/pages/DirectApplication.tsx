import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, del, post } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { StartApplicationDialog } from '../components/StartApplicationDialog'
import { StatusBadge } from '../components/StatusBadge'
import { ConfirmAction } from '../components/ConfirmAction'
import { ApplicationQuestions } from '../components/ApplicationQuestions'
import { ApplicationLog } from '../components/ApplicationLog'
import type { Application, Candidate } from '../types'

export function DirectApplication() {
  const [jobUrl, setJobUrl] = useState('')
  const [starting, setStarting] = useState(false)
  const client = useQueryClient()
  const candidates = useQuery({ queryKey: ['candidates'], queryFn: () => api<Candidate[]>('/candidate-profiles') })
  const candidateId = candidates.data?.[0]?.id
  const key = ['direct-application', candidateId]
  const active = useQuery({ queryKey: key, queryFn: () => api<Application | null>(`/applications/direct-url/active?candidate_id=${candidateId}`), enabled: !!candidateId, refetchInterval: 2_000 })
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['applications'] })
    void client.invalidateQueries({ queryKey: ['jobs'] })
    void client.invalidateQueries({ queryKey: ['direct-application'] })
  }
  const apply = useMutation({
    mutationFn: () => post<Application>('/applications/direct-url', { candidate_id: candidateId, job_url: jobUrl.trim() }),
    onSuccess: (application) => {
      client.setQueryData(key, application)
      setStarting(application.allowed_actions.includes('select_cv') || application.allowed_actions.includes('start'))
      refresh()
    },
  })
  const action = useMutation({
    mutationFn: async ({ id, name }: { id: number; name: string }) => {
      await post(`/applications/${id}/${name}`)
      return api<Application>(`/applications/${id}`)
    },
    onSuccess: (application, { name }) => {
      client.setQueryData(key, name === 'new-apply' ? null : application)
      if (name === 'new-apply') { setJobUrl(''); setStarting(false) }
      refresh()
    },
    onError: refresh,
  })
  const remove = useMutation({
    mutationFn: (id: number) => del(`/applications/${id}`),
    onSuccess: (_result, id) => {
      client.setQueryData(key, null)
      client.removeQueries({ queryKey: ['application', id] })
      setStarting(false); setJobUrl(''); refresh()
    },
  })
  const item = active.data
  const busy = action.isPending || remove.isPending
  const validUrl = /^https?:\/\/[^\s]+$/i.test(jobUrl.trim())
  const error = candidates.error ?? active.error ?? apply.error ?? action.error ?? remove.error
  return <>
    {starting && item && <StartApplicationDialog application={item} onClose={() => setStarting(false)} onStarted={() => { setStarting(false); refresh() }} />}
    <PageHeader title="Apply by URL" subtitle="Your current application stays here until you choose NEW APPLY or delete it." />
    {error && <p role="alert" className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error.message}</p>}
    {item ? <article className="card max-w-3xl">
      <div className="flex items-start justify-between gap-3"><div><p className="text-xs font-semibold uppercase text-slate-500">{item.job.company}</p><h2 className="mt-1 text-lg font-semibold">{item.job.title}</h2></div><StatusBadge status={item.status} /></div>
      <dl className="mt-5 grid grid-cols-2 gap-3 rounded-xl bg-slate-50 p-4 text-sm"><div><dt>Current website</dt><dd>{item.target_host ?? 'No live page reference — open Log for the reason'}</dd></div><div><dt>Step / fallback</dt><dd>{item.current_step ?? '—'} / L{item.fallback_level}</dd></div><div className="col-span-2"><dt>Current operation</dt><dd>{item.current_operation ?? 'Waiting'}</dd></div></dl>
      <p className="mt-3 text-sm font-medium">Suitability (advisory): {item.suitability_state ?? 'NOT_STARTED'}</p>
      <p className="mt-1 text-xs text-slate-500">Your chosen vacancy continues regardless of suitability. Missing form answers will appear below.</p>
        {item.error && <p className="mt-3 text-sm text-amber-800">{item.error}</p>}
      <ApplicationQuestions applicationId={item.id} />
      <div className="mt-4 flex flex-wrap gap-2">
        <ApplicationLog applicationId={item.id} />
        {(item.allowed_actions.includes('select_cv') || item.allowed_actions.includes('start')) && <button className="btn-primary" disabled={busy} onClick={() => setStarting(true)}>Review CV & start</button>}
        {['pause', 'resume', 'retry', 'manual', 'open-browser', 'abort'].filter((name) => item.allowed_actions.includes(name)).map((name) => <button key={name} className="btn-secondary" disabled={busy} onClick={() => action.mutate({ id: item.id, name })}>{name.replace('-', ' ')}</button>)}
        <Link className="btn-secondary" to={`/applications/${item.id}`}>Details / review</Link>
        {item.allowed_actions.includes('reset') && <ConfirmAction label="RESET APPLICATION" message="Keep this application and CV, and clear its workflow data for another run?" disabled={busy} onConfirm={() => action.mutate({ id: item.id, name: 'reset' })} />}
        {item.allowed_actions.includes('delete') && <ConfirmAction label="DELETE APPLICATION" message="Permanently delete this application and its runtime data?" danger disabled={busy} onConfirm={() => remove.mutate(item.id)} />}
        <ConfirmAction label="NEW APPLY" message="Stop unfinished work, preserve this application in history, and enter another URL?" disabled={busy || !item.allowed_actions.includes('new-apply')} onConfirm={() => action.mutate({ id: item.id, name: 'new-apply' })} />
      </div>
    </article> : <section className="card max-w-3xl">
      <label className="text-sm font-semibold" htmlFor="direct-job-url">Job URL</label>
      <div className="mt-3 flex gap-3"><input id="direct-job-url" className="field flex-1" placeholder="https://company.example/jobs/123" value={jobUrl} onChange={(event) => setJobUrl(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && validUrl && candidateId && !apply.isPending && !active.isPending) apply.mutate() }} /><button className="btn-primary" disabled={!validUrl || !candidateId || apply.isPending || active.isPending || !!active.error} onClick={() => apply.mutate()}>{apply.isPending ? 'Creating…' : 'Analyze & Apply'}</button></div>
      <p className="mt-4 text-sm text-slate-500">Select your CV before starting. Complete any human verification manually in the visible browser. Final submission requires your explicit approval.</p>
    </section>}
  </>
}

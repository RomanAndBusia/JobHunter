import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ExternalLink, Play, Save, Send } from 'lucide-react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, del, post, put } from '../api/client'
import { ConfirmAction } from '../components/ConfirmAction'
import { PageHeader } from '../components/PageHeader'
import { StatusBadge } from '../components/StatusBadge'
import type { Answer, Application } from '../types'
import { ApplicationQuestions } from '../components/ApplicationQuestions'
import { ApplicationLog } from '../components/ApplicationLog'

function AnswerRow({ applicationId, answer }: { applicationId: number; answer: Answer }) {
  const client = useQueryClient()
  const [value, setValue] = useState(typeof answer.value === 'string' ? answer.value : JSON.stringify(answer.value))
  const edit = useMutation({ mutationFn: () => put(`/applications/${applicationId}/answers/${answer.id}`, { value }), onSuccess: () => void client.invalidateQueries({ queryKey: ['application', applicationId] }) })
  return <div className="border-b border-slate-100 py-4 last:border-0">
    <label className="text-sm font-semibold">{answer.meaning || answer.question}</label>
    {answer.meaning && answer.meaning !== answer.question && <p className="mt-1 text-xs text-slate-500">{answer.question}</p>}
    <dl className="my-3 grid gap-2 rounded-xl bg-slate-50 p-3 text-xs sm:grid-cols-3">
      <div><dt className="font-semibold uppercase tracking-wide text-slate-400">Answer</dt><dd className="mt-1 break-words text-slate-800">{String(answer.value)}</dd></div>
      <div><dt className="font-semibold uppercase tracking-wide text-slate-400">Source</dt><dd className="mt-1 text-slate-800">{answer.source.replaceAll('_', ' ')}{answer.source_paths.length ? <span className="block text-slate-400">{answer.source_paths.join(', ')}</span> : null}</dd></div>
      <div><dt className="font-semibold uppercase tracking-wide text-slate-400">Status</dt><dd className={`mt-1 font-semibold ${answer.verified ? 'text-emerald-700' : 'text-amber-700'}`}>{answer.status.replaceAll('_', ' ')}</dd></div>
    </dl>
    <div className="flex gap-2"><textarea className="field min-h-11 flex-1 resize-y" value={value} onChange={(event) => setValue(event.target.value)} /><button className="btn-secondary self-start" disabled={edit.isPending || value === String(answer.value)} onClick={() => edit.mutate()}><Save size={15} /> Save to form</button></div>
    {answer.reason && <p className="mt-2 text-xs text-slate-500">{answer.reason}</p>}
    {edit.error && <p className="mt-2 text-xs text-red-600">{edit.error.message}</p>}
  </div>
}

export function ApplicationReview() {
  const params = useParams()
  const id = Number(params.id)
  const navigate = useNavigate()
  const client = useQueryClient()
  const query = useQuery({ queryKey: ['application', id], queryFn: () => api<Application>(`/applications/${id}`), enabled: Number.isFinite(id), refetchInterval: 2_000 })
  const open = useMutation({ mutationFn: () => post(`/applications/${id}/open-browser`) })
  const resume = useMutation({ mutationFn: () => post(`/applications/${id}/resume`), onSuccess: () => void query.refetch() })
  const submit = useMutation({ mutationFn: () => post<{ status: string }>(`/applications/${id}/submit`), onSuccess: (result) => { if (result.status === 'SUBMITTED') navigate('/history'); else void query.refetch() } })
  const remove = useMutation({
    mutationFn: () => del(`/applications/${id}`),
    onSuccess: () => {
      client.removeQueries({ queryKey: ['application', id] })
      client.setQueryData<Application[]>(['applications'], (items) => items?.filter((item) => item.id !== id))
      void client.invalidateQueries({ queryKey: ['applications'] }); void client.invalidateQueries({ queryKey: ['direct-application'] }); void client.invalidateQueries({ queryKey: ['jobs'] })
      navigate('/queue', { state: { notice: 'Application and its vacancy-specific runtime data were deleted.' } })
    },
  })
  const item = query.data
  const letter = item?.review_data.cover_letter as { text?: string; mode?: string; source_paths?: string[] } | undefined
  if (!item) return <div className="card">Loading application…</div>
  return <>
    <PageHeader title="Application review" subtitle="Compare this summary with the visible browser before approving." actions={<><ApplicationLog applicationId={id} />{item.allowed_actions.includes('open-browser') && <button className="btn-secondary" onClick={() => open.mutate()}><ExternalLink size={15} /> Open real form</button>}<button className="btn-primary" disabled={!item.allowed_actions.includes('submit') || submit.isPending} onClick={() => submit.mutate()}><Send size={15} /> Approve & submit</button>{item.allowed_actions.includes('delete') && <ConfirmAction label="DELETE APPLICATION" message="Permanently delete this application and its runtime data?" danger disabled={remove.isPending} onConfirm={() => remove.mutate()} />}</>} />
    <Link to="/queue" className="mb-4 inline-flex items-center gap-1 text-sm font-medium text-slate-500 hover:text-ink"><ArrowLeft size={15} /> Queue</Link>
    <div className="grid gap-6 lg:grid-cols-[1fr_320px]">
      <section className="card"><div className="mb-2 flex items-start justify-between gap-3"><div><p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{item.job.company}</p><h2 className="mt-1 text-xl font-semibold">{item.job.title}</h2></div><StatusBadge status={item.status} /></div><p className="text-sm text-slate-500">{item.job.location}</p>
        {Boolean(item.review_data.live_application_test || item.review_data.test_guard_active) && <div className="mt-4 rounded-xl bg-amber-50 p-4 text-sm text-amber-900">Test application · final submission is disabled. Answers marked Synthetic Test are temporary and are excluded from your candidate profile and reusable memory.</div>}
        {(item.current_operation || item.error) && <div className={`mt-5 rounded-xl p-4 text-sm ${item.status === 'CAPTCHA' ? 'bg-orange-50 text-orange-800' : item.error ? 'bg-red-50 text-red-700' : 'bg-blue-50 text-blue-800'}`}><div className="font-semibold">{item.current_operation}</div>{item.last_operation && item.last_operation !== item.current_operation && <p className="mt-1 text-xs">Last: {item.last_operation}</p>}{item.error && <p className="mt-1 text-xs leading-relaxed">{item.error}</p>}{item.status === 'CAPTCHA' && <button className="btn-secondary mt-3" disabled={resume.isPending} onClick={() => resume.mutate()}><Play size={14} /> Resume after verification</button>}</div>}
        <ApplicationQuestions applicationId={id} />
        {letter?.text && <div className="mt-6 rounded-xl border border-slate-200 p-4"><h3 className="font-semibold">Cover letter</h3><p className="mt-2 whitespace-pre-wrap text-sm">{letter.text}</p><p className="mt-3 text-xs text-slate-500">{letter.mode === 'txt_attachment' ? 'Text attachment' : 'Form text'} · Supported by {letter.source_paths?.join(', ')}</p></div>}
        <div className="mt-6"><h3 className="text-xs font-bold uppercase tracking-wider text-slate-400">Completed automatically · failed fields</h3>{item.answers.map((answer) => <AnswerRow key={answer.id} applicationId={id} answer={answer} />)}</div>
      </section>
      <aside className="space-y-4"><div className="card"><h3 className="font-semibold">Safety check</h3><ul className="mt-3 space-y-2 text-sm text-slate-600"><li>✓ Fields are read back after entry</li><li>✓ Current page is checked again</li><li>✓ Double submission is blocked</li><li>✓ Success must be observable</li></ul></div>{item.document_versions.length > 0 && <div className="card"><h3 className="font-semibold">Documents</h3>{item.document_versions.map((document) => <div className="mt-3" key={document.id}><div className="text-sm font-medium">{document.name}</div><div className="text-xs text-slate-500">{document.kind} v{document.version} · {document.sha256.slice(0, 10)}…</div></div>)}</div>}<div className="card"><h3 className="font-semibold">Target</h3><p className="mt-2 break-all text-xs text-slate-500">{item.target_url}</p><p className="mt-3 text-xs font-medium">{item.target_type?.replaceAll('_', ' ')}</p></div>{(submit.error || remove.error || open.error) && <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">{(submit.error ?? remove.error ?? open.error)?.message}</div>}</aside>
    </div>
  </>
}

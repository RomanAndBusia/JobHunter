import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, post } from '../api/client'
import type { QuestionRequest } from '../types'

function QuestionCard({ item }: { item: QuestionRequest }) {
  const client = useQueryClient()
  const multiple = item.field_context?.multiple ?? item.answer_type === 'multi_select'
  const [value, setValue] = useState<unknown>(multiple ? [] : '')
  const [scope, setScope] = useState('APPLICATION_ONLY')
  const answer = useMutation({
    mutationFn: (confirm: boolean) => post(`/questions/${item.id}/answer`, { value, scope, confirm_conflict: confirm }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['questions'] })
      void client.invalidateQueries({ queryKey: ['applications'] })
      void client.invalidateQueries({ queryKey: ['application'] })
    },
  })
  const booleanControl = ['boolean', 'checkbox', 'toggle'].includes(item.answer_type)
  const singleChoice = ['single_choice', 'radio'].includes(item.answer_type)
  const choices = item.options ?? []
  if (item.answer_type === 'file') return <article className="rounded-xl border border-amber-200 bg-white p-4">
    <h4 className="font-semibold">{item.question}</h4>
    <p className="mt-2 text-sm">Upload this document in the visible application page, then choose Resume. JobHunter will verify the attachment.</p>
  </article>
  return <article className="rounded-xl border border-amber-200 bg-white p-4">
    <p className="text-xs font-semibold text-slate-500">{item.question_category.replaceAll('_', ' ')}</p>
    <h4 className="mt-1 font-semibold">{item.question}</h4>
    {item.suggested_value != null && <div className="my-3 rounded-lg bg-blue-50 p-3 text-sm">
      <p className="font-semibold">AI suggestion: {String(item.suggested_value)}</p>
      <p className="mt-1">{item.suggestion_reason}</p>
      <div className="mt-2 flex gap-2"><button className="btn-secondary" onClick={() => setValue(item.suggested_value)}>Use suggestion</button><button className="btn-secondary" onClick={() => setValue(multiple ? [] : '')}>Choose another</button></div>
    </div>}
    <div className="mt-3">{booleanControl ? <div className="flex gap-4">{[true, false].map(option => <label className="flex items-center gap-2" key={String(option)}><input type="radio" name={`q-${item.id}`} checked={value === option} onChange={() => setValue(option)} />{option ? 'Yes' : 'No'}</label>)}</div>
      : singleChoice && choices.length ? <div className="space-y-2" role="radiogroup" aria-label={item.question}>{choices.map(option => <label className="flex items-center gap-2" key={option.id}><input type="radio" name={`q-${item.id}`} value={option.label} disabled={option.disabled} checked={value === option.label} onChange={() => setValue(option.label)} />{option.label}</label>)}</div>
      : choices.length ? <select aria-label={item.question} className="field" multiple={multiple} value={multiple ? (Array.isArray(value) ? value.map(String) : []) : String(value)} onChange={event => setValue(multiple ? Array.from(event.target.selectedOptions).map(option => option.value) : event.target.value)}>
        {!multiple && <option value="">Choose an answer</option>}{choices.map(option => <option key={option.id} value={option.label} disabled={option.disabled}>{option.label}</option>)}
      </select> : <textarea aria-label={item.question} className="field min-h-24" value={String(value)} onChange={event => setValue(event.target.value)} placeholder="Enter your answer" />}</div>
    <label className="mt-3 block text-xs font-semibold">Reuse<select className="field mt-1" value={scope} onChange={event => setScope(event.target.value)}>
      <option value="APPLICATION_ONLY">This application only</option><option value="CANDIDATE_GLOBAL">Global candidate fact</option><option value="JOB_PREFERENCE_DEFAULT">Reusable job preference</option>
    </select></label>
    <button className="btn-primary mt-3" disabled={answer.isPending || value === '' || (multiple && Array.isArray(value) && !value.length)} onClick={() => answer.mutate(false)}>{answer.isPending ? 'Saving…' : 'Confirm answer & continue'}</button>
    {answer.error && <p className="mt-2 text-sm text-red-700">{answer.error.message}</p>}
    {answer.error?.message.includes('conflicts') && <button className="btn-secondary mt-2" disabled={answer.isPending} onClick={() => answer.mutate(true)}>Replace the confirmed saved answer</button>}
  </article>
}

export function ApplicationQuestions({ applicationId }: { applicationId: number }) {
  const query = useQuery({ queryKey: ['questions', applicationId], queryFn: () => api<QuestionRequest[]>(`/questions/pending?application_id=${applicationId}`), refetchInterval: 3000 })
  if (query.error) return <p className="mt-3 text-sm text-red-700">{query.error.message}</p>
  if (!query.data?.length) return null
  return <section className="mt-5 space-y-3"><h3 className="font-semibold text-amber-900">Needs your input: {query.data.length}</h3>{query.data.map(item => <QuestionCard key={item.id} item={item} />)}</section>
}

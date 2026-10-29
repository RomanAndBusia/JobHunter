import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FileUp, Play, X } from 'lucide-react'
import { api, post } from '../api/client'
import type { Application, CandidateDocument } from '../types'

export function StartApplicationDialog({ application, onClose, onStarted }: {
  application: Application
  onClose: () => void
  onStarted: () => void
}) {
  const client = useQueryClient()
  const [selectedId, setSelectedId] = useState<number | ''>(application.selected_cv_document_id ?? '')
  const [file, setFile] = useState<File>()
  const documents = useQuery({
    queryKey: ['documents', application.candidate_id],
    queryFn: () => api<CandidateDocument[]>(`/candidate-profiles/${application.candidate_id}/documents`),
  })
  useEffect(() => {
    if (selectedId === '' && documents.data?.length) {
      setSelectedId((documents.data.find((item) => item.active) ?? documents.data[0]).id)
    }
  }, [documents.data, selectedId])
  const upload = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error('Choose a PDF or DOCX CV')
      const form = new FormData()
      form.append('file', file)
      return api<CandidateDocument>(`/candidate-profiles/${application.candidate_id}/documents`, { method: 'POST', body: form })
    },
    onSuccess: (document) => {
      setSelectedId(document.id)
      setFile(undefined)
      void client.invalidateQueries({ queryKey: ['documents', application.candidate_id] })
    },
  })
  const start = useMutation({
    mutationFn: async () => {
      if (selectedId === '') throw new Error('Select a CV before starting')
      const selected = await post<Application>(`/applications/${application.id}/select-cv`, { selected_cv_document_id: selectedId })
      client.setQueryData<Application[]>(['applications'], (items) => items?.map((item) => item.id === selected.id ? selected : item))
      return post<Application & { accepted: boolean; already_running: boolean }>(`/applications/${application.id}/start`)
    },
    onSuccess: (result) => {
      client.setQueryData<Application[]>(['applications'], (items) => items?.map((item) => item.id === result.id ? result : item))
      void client.invalidateQueries({ queryKey: ['applications'] })
      void client.invalidateQueries({ queryKey: ['jobs'] })
      onStarted()
    },
    onError: () => { void client.invalidateQueries({ queryKey: ['applications'] }) },
  })
  const error = documents.error ?? upload.error ?? start.error
  return <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/40 p-4">
    <section className="card w-full max-w-xl shadow-2xl" role="dialog" aria-modal="true" aria-label="Start application">
      <div className="flex items-start justify-between gap-4"><div><h2 className="text-lg font-semibold">Start application</h2><p className="mt-1 text-sm text-slate-500">{application.job.company} · {application.job.title}</p><p className="mt-1 text-xs text-slate-500">Candidate profile #{application.candidate_id}</p></div><button className="btn-secondary !p-2" onClick={onClose}><X size={16} /></button></div>
      <label className="mt-5 block text-sm font-semibold" htmlFor="application-cv">CV for this application</label>
      <select id="application-cv" className="field mt-2 w-full" value={selectedId} onChange={(event) => setSelectedId(Number(event.target.value))}>
        <option value="">Select a stored CV…</option>
        {documents.data?.map((document) => <option key={document.id} value={document.id}>{document.name} · v{document.version} · {new Date(document.created_at).toLocaleDateString()} · profile #{document.profile_id}{document.active ? ' · default' : ''}</option>)}
      </select>
      <div className="mt-4 rounded-xl border border-slate-200 p-4"><p className="text-sm font-semibold">Upload another CV</p><div className="mt-2 flex flex-col gap-2 sm:flex-row"><input className="field flex-1" type="file" accept=".pdf,.docx" onChange={(event) => setFile(event.target.files?.[0])} /><button className="btn-secondary" disabled={!file || upload.isPending} onClick={() => upload.mutate()}><FileUp size={15} /> {upload.isPending ? 'Importing…' : 'Import'}</button></div></div>
      {error && <p className="mt-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error.message}</p>}
      <div className="mt-5 flex justify-end gap-2"><button className="btn-secondary" onClick={onClose}>Cancel</button><button className="btn-primary" disabled={selectedId === '' || start.isPending} onClick={() => start.mutate()}><Play size={15} /> {start.isPending ? 'Starting…' : 'Start application'}</button></div>
    </section>
  </div>
}

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FileUp } from 'lucide-react'
import { api } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import type { Candidate } from '../types'

type Document = { id: number; kind: string; version: number; name: string; sha256: string; active: boolean; created_at: string }

export function Documents() {
  const client = useQueryClient()
  const [file, setFile] = useState<File>()
  const candidates = useQuery({ queryKey: ['candidates'], queryFn: () => api<Candidate[]>('/candidate-profiles') })
  const id = candidates.data?.[0]?.id
  const documents = useQuery({ queryKey: ['documents', id], queryFn: () => api<Document[]>(`/candidate-profiles/${id}/documents`), enabled: Boolean(id) })
  const upload = useMutation({
    mutationFn: async () => {
      if (!id || !file) throw new Error('Select a PDF or DOCX document')
      const form = new FormData(); form.append('file', file)
      return api(`/candidate-profiles/${id}/documents`, { method: 'POST', body: form })
    },
    onSuccess: () => { setFile(undefined); void client.invalidateQueries({ queryKey: ['documents', id] }) },
  })
  return <><PageHeader title="Documents" subtitle="Versioned CVs remain traceable to every submitted application." /><div className="grid gap-6 lg:grid-cols-[360px_1fr]"><section className="card"><div className="grid h-12 w-12 place-items-center rounded-xl bg-blue-50 text-blue-700"><FileUp /></div><h2 className="mt-4 font-semibold">Upload a new CV version</h2><p className="mt-2 text-sm leading-relaxed text-slate-500">PDF and DOCX, up to 10 MB. The previous version remains in history but becomes inactive.</p><input className="field mt-5" type="file" accept=".pdf,.docx" onChange={(e) => setFile(e.target.files?.[0])} /><button className="btn-primary mt-3 w-full" disabled={!file || upload.isPending} onClick={() => upload.mutate()}>{upload.isPending ? 'Importing…' : 'Import document'}</button>{upload.error && <p className="mt-3 text-xs text-red-600">{upload.error.message}</p>}</section><section className="card"><h2 className="font-semibold">Document versions</h2><div className="mt-4 space-y-3">{documents.data?.map((doc) => <div className="flex items-center justify-between rounded-xl border border-slate-200 p-4" key={doc.id}><div><div className="text-sm font-semibold">{doc.name}</div><div className="mt-1 text-xs text-slate-500">CV v{doc.version} · {new Date(doc.created_at).toLocaleString()} · {doc.sha256.slice(0, 10)}…</div></div><span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${doc.active ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>{doc.active ? 'Active' : 'Archived'}</span></div>)}{!documents.data?.length && <p className="py-10 text-center text-sm text-slate-500">No documents uploaded.</p>}</div></section></div></>
}

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

export function ApplicationLog({ applicationId }: { applicationId: number }) {
  const [open, setOpen] = useState(false)
  const [full, setFull] = useState(false)
  const [notice, setNotice] = useState('')
  const query = useQuery({ queryKey: ['application-log', applicationId, full], queryFn: () => api<Record<string, unknown>>(`/applications/${applicationId}/log?full_run=${full}`), enabled: open, refetchInterval: open ? 2_000 : false })
  const text = JSON.stringify(query.data ?? {}, null, 2)
  const copy = async () => {
    try { await navigator.clipboard.writeText(text); setNotice('Copied') }
    catch { setNotice('Clipboard unavailable; select and copy the diagnostic text below.') }
  }
  const save = () => {
    const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }))
    const link = document.createElement('a'); link.href = url; link.download = `jobhunter-application-${applicationId}-logs.json`; link.click(); URL.revokeObjectURL(url)
  }
  return <>
    <button className="btn-secondary" onClick={() => setOpen(true)}>Log</button>
    {open && <div role="dialog" aria-modal="true" aria-label="Application Log" className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"><section className="max-h-[90vh] w-full max-w-5xl overflow-y-auto rounded-2xl bg-white p-6 shadow-xl">
      <div className="flex items-center justify-between"><h2 className="text-xl font-semibold">Log · application {applicationId}</h2><button className="btn-secondary" onClick={() => setOpen(false)}>Close</button></div>
      <div className="my-4 flex flex-wrap items-center gap-3"><label className="text-sm"><input type="checkbox" checked={full} onChange={e => setFull(e.target.checked)} /> Full current run</label><button className="btn-secondary" disabled={!query.data} onClick={() => void copy()}>Copy logs</button><button className="btn-secondary" disabled={!query.data} onClick={save}>Save logs</button><span role="status" className="text-sm">{notice}</span></div>
      {query.error && <p role="alert" className="text-red-700">{query.error.message}</p>}
      {query.isLoading && <p>Loading diagnostics…</p>}
      {Object.entries(query.data ?? {}).filter(([key]) => key !== 'events').map(([key, value]) => <details key={key} open={['browser', 'vacancy', 'suitability', 'pipeline', 'challenge'].includes(key)} className="mb-3 rounded-lg border p-3"><summary className="cursor-pointer font-semibold capitalize">{key.replaceAll('_', ' ')}</summary><pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words text-xs">{JSON.stringify(value, null, 2)}</pre></details>)}
      <details className="rounded-lg border p-3"><summary className="cursor-pointer font-semibold">Events and expandable error stack traces</summary>{(query.data?.events as Array<Record<string, unknown>> | undefined)?.map((record, index) => <details key={index} className="mt-2 border-t p-2"><summary className="cursor-pointer text-xs">{String(record.timestamp ?? '')} · {String(record.level ?? '')} · {String(record.message ?? '')}</summary><pre className="mt-2 whitespace-pre-wrap break-words text-xs">{JSON.stringify(record, null, 2)}</pre></details>)}</details>
    </section></div>}
  </>
}

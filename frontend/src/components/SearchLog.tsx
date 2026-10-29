import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

export function SearchLog({ runId }: { runId: number }) {
  const [open, setOpen] = useState(false)
  const [notice, setNotice] = useState('')
  const log = useQuery({ queryKey: ['search-log', runId], queryFn: () => api<{events: Record<string, unknown>[]}>(`/justjoin/search-runs/${runId}/log`), enabled: open, refetchInterval: open ? 2000 : false })
  const text = JSON.stringify(log.data ?? {}, null, 2)
  const copy = async () => {try {await navigator.clipboard.writeText(text); setNotice('Copied')} catch {setNotice('Clipboard unavailable')}}
  const save = () => {const url=URL.createObjectURL(new Blob([text],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=`jobhunter-search-${runId}-log.json`;a.click();URL.revokeObjectURL(url)}
  return <><button className="btn-secondary" onClick={() => setOpen(true)}>Log</button>{open && <div className="fixed inset-0 z-50 grid place-items-center bg-slate-900/50 p-5" role="dialog" aria-modal="true" aria-label="Search Log"><section className="max-h-[85vh] w-full max-w-4xl overflow-auto rounded-2xl bg-white p-6"><div className="mb-5 flex items-center justify-between"><h2 className="font-semibold">Search log · {runId}</h2><button className="btn-secondary" onClick={() => setOpen(false)}>Close</button></div><div className="mb-4 flex items-center gap-3"><button className="btn-secondary" disabled={!log.data} onClick={()=>void copy()}>Copy logs</button><button className="btn-secondary" disabled={!log.data} onClick={save}>Save logs</button><span role="status" className="text-sm">{notice}</span></div>{log.error && <p role="alert">{log.error.message}</p>}{log.data?.events.map((event, i) => <details key={i} className="border-t py-3"><summary className="cursor-pointer text-sm">{String(event.timestamp ?? '')} · {String(event.message ?? '')}</summary><pre className="overflow-auto whitespace-pre-wrap break-words text-xs">{JSON.stringify(event, null, 2)}</pre></details>)}{!log.data?.events.length && <p className="text-sm text-slate-500">No retained events. Persisted progress remains available.</p>}</section></div>}</>
}

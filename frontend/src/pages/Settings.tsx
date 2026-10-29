import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Play, Plus, ScanSearch, Sparkles, Trash2 } from 'lucide-react'
import { api, del, post } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import type { Candidate, SearchProfile } from '../types'

type BrowserStatus = { running: boolean; paused: boolean; session_key: string; pages: Array<{ id: string; url: string }>; cdp: { mode: string; endpoint: string | null; headed: boolean }; stealth: { configured: boolean; available: boolean | null; applied: boolean; error_message: string | null }; challenge: { provider: string | null; url: string } | null }
type KnownAnswer = { id: number; concept: string; value: unknown; source: string; scope: string }
type Campaign = { progress?: { current_query?: string; queued?: number; errors?: number }; id: number; name: string; status: string; families: string[]; queries: Array<{ id: number; family: string; query: string }> }
type AIHealth = {
  runtime: string
  provider: string
  model: string
  reasoning_effort: string
  max_output_tokens: number
  api_key_present: boolean
  configuration_valid: boolean
  provider_reachable: boolean | null
  configured: boolean
  authenticated: boolean | null
  inference: boolean | null
  endpoint: string | null
  http_status: number | null
  request_id: string | null
  retry_count: number
  response_parsed: boolean | null
  status: string
  last_check: string | null
  error_type: string | null
  error_message: string | null
}
type ProductionAIHealth = {
  runtime: string
  model: string
  status: string
  api_authentication: boolean
  openai_inference: boolean
  production_ai_service: boolean
  structured_output: boolean
  database_persistence: boolean
  stage: string
  request_id: string | null
  http_status: number | null
  error_type: string | null
  error_message: string | null
}

export function Settings() {
  const client = useQueryClient()
  const [name, setName] = useState('')
  const [queryText, setQueryText] = useState('')
  const [location, setLocation] = useState('')
  const [workModes, setWorkModes] = useState('')
  const [employmentTypes, setEmploymentTypes] = useState('')
  const [seniority, setSeniority] = useState('')
  const profiles = useQuery({ queryKey: ['search-profiles'], queryFn: () => api<SearchProfile[]>('/search-profiles') })
  const candidates = useQuery({ queryKey: ['candidates'], queryFn: () => api<Candidate[]>('/candidate-profiles') })
  const candidateId = candidates.data?.[0]?.id
  const answers = useQuery({ queryKey: ['known-answers', candidateId], queryFn: () => api<KnownAnswer[]>(`/candidate-profiles/${candidateId}/known-answers`), enabled: Boolean(candidateId) })
  const campaigns = useQuery({ queryKey: ['campaigns'], queryFn: () => api<Campaign[]>('/search-campaigns'), refetchInterval: 2_000 })
  const browser = useQuery({ queryKey: ['browser'], queryFn: () => api<BrowserStatus>('/browser'), refetchInterval: 5_000 })
  const aiHealth = useQuery({ queryKey: ['ai-health'], queryFn: () => api<AIHealth>('/v1/ai/health?probe=false'), retry: false })
  const scan = useMutation({ mutationFn: (id: number) => post(`/scans/${id}`), onSuccess: () => { void client.invalidateQueries({ queryKey: ['dashboard'] }); void client.invalidateQueries({ queryKey: ['jobs'] }) } })
  const startBrowser = useMutation({ mutationFn: () => post('/browser/start'), onSuccess: () => void browser.refetch() })
  const testAI = useMutation({
    mutationFn: () => post<AIHealth>('/v1/ai/health/self-test'),
    onSuccess: (result) => client.setQueryData(['ai-health'], result),
  })
  const testProductionAI = useMutation({ mutationFn: () => post<ProductionAIHealth>('/v1/ai/production-self-test') })
  const createCampaign = useMutation({ mutationFn: () => post('/search-campaigns', { candidate_id: candidateId, name: 'CV-driven IT search' }), onSuccess: () => void client.invalidateQueries({ queryKey: ['campaigns'] }) })
  const scanCampaign = useMutation({ mutationFn: (id: number) => post(`/search-campaigns/${id}/scan`), onSuccess: () => { void client.invalidateQueries({ queryKey: ['campaigns'] }); void client.invalidateQueries({ queryKey: ['jobs'] }) } })
  const forget = useMutation({ mutationFn: (id: number) => del(`/candidate-profiles/${candidateId}/known-answers/${id}`), onSuccess: () => void client.invalidateQueries({ queryKey: ['known-answers'] }) })
  const values = (text: string) => text.split(',').map((item) => item.trim()).filter(Boolean)
  const add = useMutation({ mutationFn: () => post('/search-profiles', { name, query: queryText, location: location || null, work_modes: values(workModes), employment_types: values(employmentTypes), seniority: values(seniority), max_pages: 2, enabled: true }), onSuccess: () => { setName(''); setQueryText(''); setLocation(''); setWorkModes(''); setEmploymentTypes(''); setSeniority(''); void client.invalidateQueries({ queryKey: ['search-profiles'] }) } })
  return <>
    <PageHeader title="Settings" subtitle="Search coverage and the visible browser session." />
    <div className="grid gap-6 xl:grid-cols-[1fr_360px]">
      <section className="card">
        <h2 className="font-semibold">Just Join IT search profiles</h2>
        <p className="mt-1 text-sm text-slate-500">Overlapping profiles are safe; source IDs deduplicate their results.</p>
        <div className="mt-5 space-y-2">
          {profiles.data?.map((profile) => <div className="flex flex-col justify-between gap-3 rounded-xl border border-slate-200 p-4 sm:flex-row sm:items-center" key={profile.id}>
            <div>
              <div className="text-sm font-semibold">{profile.name}</div>
              <div className="mt-1 text-xs text-slate-500">{profile.query} {profile.location ? `· ${profile.location}` : ''} · up to {profile.max_pages} pages</div>
              <div className="mt-1 text-[11px] text-slate-400">{[...profile.work_modes, ...profile.employment_types, ...profile.seniority].join(' · ') || 'No additional constraints'}</div>
            </div>
            <button className="btn-secondary" disabled={scan.isPending} onClick={() => scan.mutate(profile.id)}><ScanSearch size={15} /> Scan now</button>
          </div>)}
        </div>
        <div className="mt-6 border-t border-slate-200 pt-5">
          <h3 className="text-sm font-semibold">Add search profile</h3>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            <input className="field" placeholder="Profile name" value={name} onChange={(e) => setName(e.target.value)} />
            <input className="field" placeholder="Search terms" value={queryText} onChange={(e) => setQueryText(e.target.value)} />
            <input className="field" placeholder="Location (optional)" value={location} onChange={(e) => setLocation(e.target.value)} />
            <input className="field" placeholder="Modes: remote, hybrid" value={workModes} onChange={(e) => setWorkModes(e.target.value)} />
            <input className="field" placeholder="Employment types, comma-separated" value={employmentTypes} onChange={(e) => setEmploymentTypes(e.target.value)} />
            <input className="field" placeholder="Seniority: internship, entry" value={seniority} onChange={(e) => setSeniority(e.target.value)} />
          </div>
          <button className="btn-primary mt-3" disabled={!name || !queryText || add.isPending} onClick={() => add.mutate()}><Plus size={15} /> Add profile</button>
        </div>
        {scanCampaign.error && <p className="mt-3 text-sm text-red-600">{scanCampaign.error.message}</p>}{scan.error && <p className="mt-4 text-sm text-red-600">{scan.error.message}</p>}
        <div className="mt-7 border-t border-slate-200 pt-5"><div className="flex items-center justify-between gap-3"><div><h3 className="text-sm font-semibold">Multi-role search campaign</h3><p className="mt-1 text-xs text-slate-500">Generate role families and non-duplicative queries from the CV.</p></div><button className="btn-secondary" disabled={!candidateId || createCampaign.isPending} onClick={() => createCampaign.mutate()}><Sparkles size={15} /> Generate plan</button></div><div className="mt-3 space-y-3">{campaigns.data?.map((campaign) => <div className="rounded-xl border border-slate-200 p-4" key={campaign.id}><div className="flex items-center justify-between"><div><div className="text-sm font-semibold">{campaign.name}</div><div className="mt-1 text-xs text-slate-500">{campaign.families.join(' · ')}</div><p className="mt-2 text-xs text-slate-600">{campaign.status.replaceAll('_', ' ')} · {campaign.progress?.current_query ?? ''} · {campaign.progress?.queued ?? 0} queued · {campaign.progress?.errors ?? 0} errors</p></div><button className="btn-secondary" disabled={scanCampaign.isPending || ['SEARCHING', 'EVALUATING', 'PREPARING_APPLICATIONS'].includes(campaign.status)} onClick={() => scanCampaign.mutate(campaign.id)}><ScanSearch size={14} /> Search & prepare {campaign.queries.length} queries</button></div></div>)}</div></div>
      </section>
      <aside className="space-y-4">
        <div className="card">
          <div className="flex items-center justify-between"><h2 className="font-semibold">Browser session</h2><span className={`h-2.5 w-2.5 rounded-full ${browser.data?.running ? 'bg-emerald-500' : 'bg-slate-300'}`} /></div>
          <p className="mt-2 text-sm text-slate-500">{browser.data?.running ? `${browser.data.pages.length} tracked pages · ${browser.data.paused ? 'paused' : 'active'}` : 'Google Chrome is not connected.'}</p>
          {browser.data && <p className="mt-2 text-xs text-slate-500">Google Chrome · CDP · headed · {browser.data.cdp?.endpoint ?? 'not connected'}</p>}
          {browser.data?.challenge && <p className="mt-3 rounded-lg bg-orange-50 p-3 text-xs font-semibold text-orange-800">Waiting for manual verification{browser.data.challenge.provider ? ` · ${browser.data.challenge.provider}` : ''}…</p>}
          {!browser.data?.running && <button className="btn-primary mt-4 w-full" onClick={() => startBrowser.mutate()}><Play size={15} /> Start visible browser</button>}
        </div>
        <div className="card">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="font-semibold">AI Diagnostics</h2>
              <p className="mt-1 text-xs text-slate-500">A real minimal request through the active runtime.</p>
            </div>
            <span className={`h-2.5 w-2.5 rounded-full ${aiHealth.data?.configuration_valid ? 'bg-emerald-500' : 'bg-red-500'}`} />
          </div>
          {aiHealth.data && <dl className="mt-4 grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
            <dt className="text-slate-500">Runtime</dt><dd className="text-right font-semibold">{aiHealth.data.runtime}</dd>
            <dt className="text-slate-500">Provider</dt><dd className="text-right font-semibold">{aiHealth.data.provider}</dd>
            <dt className="text-slate-500">Model</dt><dd className="truncate text-right font-semibold" title={aiHealth.data.model}>{aiHealth.data.model}</dd>
            <dt className="text-slate-500">Reasoning</dt><dd className="text-right font-semibold">{aiHealth.data.reasoning_effort}</dd>
            <dt className="text-slate-500">API key</dt><dd className="text-right font-semibold">{aiHealth.data.api_key_present ? 'Present' : 'Missing'}</dd>
            <dt className="text-slate-500">Authentication</dt><dd className="text-right font-semibold">{aiHealth.data.authenticated === null ? 'Not tested' : aiHealth.data.authenticated ? 'Healthy' : 'Failed'}</dd>
            <dt className="text-slate-500">Inference</dt><dd className="text-right font-semibold">{aiHealth.data.inference === null ? 'Not tested' : aiHealth.data.inference ? 'Healthy' : 'Failed'}</dd>
            <dt className="text-slate-500">Endpoint</dt><dd className="truncate text-right font-semibold" title={aiHealth.data.endpoint ?? ''}>{aiHealth.data.endpoint ? new URL(aiHealth.data.endpoint).host : 'Codex CLI'}</dd>
            {aiHealth.data.http_status !== null && <><dt className="text-slate-500">HTTP</dt><dd className="text-right font-semibold">{aiHealth.data.http_status}</dd></>}
            {aiHealth.data.request_id && <><dt className="text-slate-500">Request ID</dt><dd className="truncate text-right font-semibold" title={aiHealth.data.request_id}>{aiHealth.data.request_id}</dd></>}
            {aiHealth.data.last_check && <><dt className="text-slate-500">Last check</dt><dd className="text-right font-semibold">{new Date(aiHealth.data.last_check).toLocaleTimeString()}</dd></>}
          </dl>}
          {(aiHealth.data?.error_message || aiHealth.error || testAI.error) && <p className="mt-3 rounded-lg bg-red-50 p-3 text-xs leading-relaxed text-red-700">{aiHealth.data?.error_message ?? aiHealth.error?.message ?? testAI.error?.message}</p>}
          <button className="btn-secondary mt-4 w-full" disabled={testAI.isPending || !aiHealth.data?.configuration_valid} onClick={() => testAI.mutate()}><Activity size={15} /> {testAI.isPending ? 'Testing…' : 'Test AI connection'}</button>
          {testProductionAI.data && <div className="mt-4 rounded-xl bg-slate-50 p-3">
            <div className="mb-2 text-xs font-semibold">Production path · {testProductionAI.data.status}</div>
            <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
              {[
                ['API authentication', testProductionAI.data.api_authentication],
                ['OpenAI inference', testProductionAI.data.openai_inference],
                ['Production service', testProductionAI.data.production_ai_service],
                ['Structured output', testProductionAI.data.structured_output],
                ['Database persistence', testProductionAI.data.database_persistence],
              ].map(([label, ok]) => <div className="contents" key={String(label)}><dt className="text-slate-500">{label}</dt><dd className={`text-right font-semibold ${ok ? 'text-emerald-700' : 'text-red-700'}`}>{ok ? 'Passed' : 'Failed'}</dd></div>)}
            </dl>
            {testProductionAI.data.error_message && <p className="mt-2 text-xs text-red-700">{testProductionAI.data.error_message} [{testProductionAI.data.stage}]</p>}
          </div>}
          {testProductionAI.error && <p className="mt-3 rounded-lg bg-red-50 p-3 text-xs text-red-700">{testProductionAI.error.message}</p>}
          <button className="btn-secondary mt-2 w-full" disabled={testProductionAI.isPending || !aiHealth.data?.configuration_valid} onClick={() => testProductionAI.mutate()}><Sparkles size={15} /> {testProductionAI.isPending ? 'Testing production…' : 'Production AI smoke test'}</button>
        </div>
        <div className="card"><h2 className="font-semibold">Known answers</h2><p className="mt-1 text-xs text-slate-500">Semantic facts reused across equivalent application questions.</p><div className="mt-4 space-y-3">{answers.data?.map((answer) => <div className="flex items-start justify-between gap-3 rounded-xl bg-slate-50 p-3" key={answer.id}><div><div className="text-sm font-semibold">{answer.concept.replaceAll('_', ' ')}</div><div className="mt-1 text-xs text-slate-600">{JSON.stringify(answer.value)} · {answer.source.replaceAll('_', ' ')}</div></div><button className="text-slate-400 hover:text-red-600" onClick={() => forget.mutate(answer.id)} aria-label="Forget answer"><Trash2 size={15} /></button></div>)}{!answers.data?.length && <p className="text-sm text-slate-400">No remembered answers yet.</p>}</div></div>
      </aside>
    </div>
  </>
}

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, FileUp, Save } from 'lucide-react'
import { api, put } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import type { Candidate, CandidateFact } from '../types'

const sections = ['identity', 'contact', 'location', 'professional_summary', 'employment', 'education', 'projects', 'skills', 'skill_groups', 'languages', 'certifications', 'links', 'preferences', 'career_goal', 'work_authorization'] as const
const titles: Record<string, string> = { identity: 'Personal', contact: 'Contact', location: 'Location', professional_summary: 'Professional summary', employment: 'Experience', education: 'Education', projects: 'Projects', skills: 'Skills', skill_groups: 'Skill groups', languages: 'Languages', certifications: 'Certifications', links: 'Links', preferences: 'Job preferences', career_goal: 'Career information', work_authorization: 'Known application facts' }

export function Profile() {
  const client = useQueryClient()
  const candidates = useQuery({ queryKey: ['candidates'], queryFn: () => api<Candidate[]>('/candidate-profiles') })
  const profile = candidates.data?.[0]
  const facts = useQuery({ queryKey: ['candidate-facts', profile?.id], queryFn: () => api<CandidateFact[]>(`/candidate-profiles/${profile?.id}/facts`), enabled: Boolean(profile?.id) })
  const [name, setName] = useState('Default candidate')
  const [data, setData] = useState<Record<string, unknown>>({})
  const [file, setFile] = useState<File>()
  const [error, setError] = useState('')
  useEffect(() => { if (profile) { setName(profile.name); setData(profile.data) } }, [profile])
  const provenance = useMemo(() => new Map((facts.data ?? []).map((fact) => [fact.path.split('.')[0], fact.source])), [facts.data])
  const save = useMutation({ mutationFn: (confirmed: boolean) => put(`/candidate-profiles/${profile?.id}`, { name, data, confirmed }), onSuccess: () => { setError(''); void client.invalidateQueries({ queryKey: ['candidates'] }); void client.invalidateQueries({ queryKey: ['candidate-facts'] }) }, onError: (e: Error) => setError(e.message) })
  const upload = useMutation({ mutationFn: async () => { if (!profile?.id || !file) throw new Error('Select a PDF or DOCX CV'); const form = new FormData(); form.append('file', file); return api(`/candidate-profiles/${profile.id}/documents`, { method: 'POST', body: form }) }, onSuccess: () => { setFile(undefined); void client.invalidateQueries({ queryKey: ['candidates'] }); void client.invalidateQueries({ queryKey: ['candidate-facts'] }) } })
  const updateSection = (key: string, value: string) => { try { setData((current) => ({ ...current, [key]: JSON.parse(value) })); setError('') } catch { setError(`${titles[key]} must be valid JSON`) } }
  return <>
    <PageHeader title="Candidate profile" subtitle="Upload a CV first; JobHunter extracts structured facts, skills, languages, and role signals automatically." actions={<><button className="btn-secondary" disabled={!profile || save.isPending} onClick={() => save.mutate(false)}><Save size={15} /> Save edits</button><button className="btn-primary" disabled={!profile || save.isPending} onClick={() => save.mutate(true)}><CheckCircle2 size={15} /> Confirm facts</button></>} />
    <section className="card mb-6 border-blue-200 bg-blue-50/40"><div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between"><div><div className="flex items-center gap-2 font-semibold"><FileUp size={18} /> CV-first setup</div><p className="mt-1 text-sm text-slate-600">PDF or DOCX, up to 10 MB. Import populates empty profile sections without overwriting your confirmed edits.</p></div><div className="flex min-w-[300px] gap-2"><input className="field bg-white" type="file" accept=".pdf,.docx" onChange={(event) => setFile(event.target.files?.[0])} /><button className="btn-primary whitespace-nowrap" disabled={!file || upload.isPending} onClick={() => upload.mutate()}>{upload.isPending ? 'Reading…' : 'Import CV'}</button></div></div>{upload.error && <p className="mt-3 text-sm text-red-600">{upload.error.message}</p>}</section>
    <div className="mb-6 card"><label className="label">Profile name</label><input className="field max-w-xl" value={name} onChange={(event) => setName(event.target.value)} /></div>
    <div className="grid gap-5 xl:grid-cols-2">{sections.map((key) => <section className="card" key={key}><div className="mb-3 flex items-center justify-between"><h2 className="font-semibold">{titles[key]}</h2><span className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-semibold text-slate-600">{(provenance.get(key) ?? 'PROFILE').replaceAll('_', ' ')}</span></div><textarea className="field min-h-[150px] font-mono text-xs leading-relaxed" spellCheck={false} value={JSON.stringify(data[key] ?? (['employment', 'education', 'projects', 'skills', 'languages'].includes(key) ? [] : {}), null, 2)} onChange={(event) => updateSection(key, event.target.value)} /></section>)}</div>
    {error && <p className="mt-4 rounded-xl bg-red-50 p-3 text-sm text-red-700">{error}</p>}
  </>
}

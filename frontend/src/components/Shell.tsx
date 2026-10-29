import { Bot, BriefcaseBusiness, ClipboardCheck, FileClock, FileText, Gauge, Link, Menu, Radar, Settings, UserRound, Wrench } from 'lucide-react'
import { NavLink, Outlet } from 'react-router-dom'
import { useUI } from '../stores/ui'
import { useEvents } from '../hooks/useEvents'

const links = [
  ['/', 'Dashboard', Gauge],
  ['/jobs', 'Search', Radar],
  ['/recommendations', 'Recommendations', BriefcaseBusiness],
  ['/apply-url', 'Apply by URL', Link],
  ['/queue', 'Applications', ClipboardCheck],
  ['/codex', 'Codex agent', Bot],
  ['/history', 'History', FileClock],
  ['/profile', 'Candidate profile', UserRound],
  ['/documents', 'Documents', FileText],
  ['/settings', 'Settings', Settings],
  ['/diagnostics', 'Diagnostics', Wrench],
] as const

export function Shell() {
  const { navOpen, setNavOpen } = useUI()
  useEvents()
  return <div className="min-h-screen bg-paper">
    <header className="fixed inset-x-0 top-0 z-40 flex h-16 items-center border-b border-slate-200 bg-white/90 px-4 backdrop-blur lg:hidden">
      <button className="btn-secondary !p-2" onClick={() => setNavOpen(!navOpen)} aria-label="Toggle navigation"><Menu size={19} /></button>
      <div className="ml-3 flex items-center gap-2 font-bold"><BriefcaseBusiness size={20} /> JobHunter</div>
    </header>
    <aside className={`fixed inset-y-0 left-0 z-50 w-64 border-r border-slate-200 bg-white px-4 py-6 transition-transform lg:translate-x-0 ${navOpen ? 'translate-x-0' : '-translate-x-full'}`}>
      <div className="mb-9 flex items-center gap-3 px-2">
        <div className="grid h-10 w-10 place-items-center rounded-xl bg-ink text-white"><BriefcaseBusiness size={20} /></div>
        <div><div className="font-bold tracking-tight">JobHunter</div><div className="text-xs text-slate-500">Application cockpit</div></div>
      </div>
      <nav className="space-y-1">
        {links.map(([to, label, Icon]) => <NavLink key={to} to={to} end={to === '/'} onClick={() => setNavOpen(false)} className={({ isActive }) => `flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition ${isActive ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100 hover:text-ink'}`}>
          <Icon size={18} /> {label}
        </NavLink>)}
      </nav>
      <div className="absolute bottom-5 left-4 right-4 rounded-xl bg-blue-50 p-3 text-xs leading-relaxed text-blue-800">
        Final submission always requires your approval. Browser activity stays visible.
      </div>
    </aside>
    {navOpen && <button className="fixed inset-0 z-40 bg-black/20 lg:hidden" onClick={() => setNavOpen(false)} aria-label="Close navigation" />}
    <main className="px-4 pb-14 pt-24 lg:ml-64 lg:px-10 lg:pt-10"><div className="mx-auto max-w-7xl"><Outlet /></div></main>
  </div>
}

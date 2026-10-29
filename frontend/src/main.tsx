import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createBrowserRouter, RouterProvider, Navigate } from 'react-router-dom'
import { Shell } from './components/Shell'
import { ApplicationQueue } from './pages/ApplicationQueue'
import { ApplicationReview } from './pages/ApplicationReview'
import { Dashboard } from './pages/Dashboard'
import { Diagnostics } from './pages/Diagnostics'
import { Documents } from './pages/Documents'
import { History } from './pages/History'
import { Jobs } from './pages/Jobs'
import { Profile } from './pages/Profile'
import { Settings } from './pages/Settings'
import { CodexAgent } from './pages/CodexAgent'
import { DirectApplication } from './pages/DirectApplication'
import './index.css'

const client = new QueryClient({ defaultOptions: { queries: { staleTime: 5_000, retry: 1 } } })
const router = createBrowserRouter([{ path: '/', element: <Shell />, children: [
  { index: true, element: <Dashboard /> },
  { path: 'jobs', element: <Jobs /> },
  { path: 'recommendations', element: <Jobs recommendations /> },
  { path: 'apply-url', element: <DirectApplication /> },
  { path: 'queue', element: <ApplicationQueue /> },
  { path: 'questions', element: <Navigate to="/queue" replace /> },
  { path: 'codex', element: <CodexAgent /> },
  { path: 'applications/:id', element: <ApplicationReview /> },
  { path: 'history', element: <History /> },
  { path: 'profile', element: <Profile /> },
  { path: 'documents', element: <Documents /> },
  { path: 'settings', element: <Settings /> },
  { path: 'diagnostics', element: <Diagnostics /> },
] }])

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></React.StrictMode>)

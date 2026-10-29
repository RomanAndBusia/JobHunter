export type Job = {
  id: number
  source: string
  source_job_id: string
  canonical_url: string
  title: string
  company: string
  location: string
  work_mode?: string
  employment_type?: string
  seniority?: string
  category?: string
  requirements: string[]
  responsibilities: string[]
  compact_json: Record<string, unknown>
  evaluation?: { decision?: string; suitability_score?: number; confidence?: number; reasons?: string[]; hard_blockers?: string[]; missing_skills?: string[]; unknowns?: string[] }
  matching_status: string
  application_status: string
  first_seen_at: string
  last_seen_at: string
}

export type Candidate = {
  id: number
  name: string
  data: Record<string, unknown>
  confirmed_at?: string
  updated_at: string
}

export type Answer = {
  id: number
  field_id: string
  question: string
  value: unknown
  source: string
  verified: boolean
  manual_override: boolean
  meaning?: string
  source_paths: string[]
  confidence?: number
  execution_confidence?: 'EXACT' | 'HIGH_CONFIDENCE' | 'AMBIGUOUS' | 'UNKNOWN'
  reason?: string
  status: string
}

export type Application = {
  id: number
  candidate_id: number
  job_id: number
  url_fingerprint?: string
  selected_cv_document_id?: number
  direct_slot_active?: boolean | null
  allowed_actions: string[]
  status: string
  suitability_state?: string
  target_type?: string
  target_host?: string
  target_url?: string
  current_step?: string
  current_operation?: string
  last_operation?: string
  fallback_level: number
  document_versions: Array<{ id: number; kind: string; version: number; name: string; sha256: string }>
  review_data: Record<string, unknown>
  error?: string
  started_at?: string
  reviewed_at?: string
  submitted_at?: string
  created_at: string
  job: { id: number; title: string; company: string; location: string; source: string; url: string; provider?: string }
  answers: Answer[]
  steps: Array<Record<string, unknown>>
}

export type CandidateDocument = {
  id: number
  profile_id: number
  name: string
  kind: string
  version: number
  sha256: string
  active: boolean
  created_at: string
}

export type SearchProfile = {
  id: number
  name: string
  query: string
  location?: string
  work_modes: string[]
  employment_types: string[]
  seniority: string[]
  max_pages: number
  enabled: boolean
  created_at: string
}

export type CandidateFact = {
  id: number
  path: string
  concept?: string
  value: unknown
  source: string
  confidence: number
  user_confirmed: boolean
}

export type QuestionRequest = {
  id: number
  application_id: number
  candidate_id: number
  field_id: string
  question: string
  concept?: string
  fact_path?: string
  answer_type: string
  options: Array<{ id: string; label: string; disabled?: boolean }>
  field_context?: { required?: boolean; multiple?: boolean; page?: string; step?: string; semantic_key?: string }
  scope: string
  question_category: string
  suggested_value?: unknown
  suggestion_reason?: string
  status: string
  conflict?: { stored: unknown; new: unknown }
  created_at: string
}

export type CodexRun = {
  id: number
  task: string
  status: string
  progress: Record<string, unknown>
  result: Record<string, unknown>
  error?: string
  created_at: string
}

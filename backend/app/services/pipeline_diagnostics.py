from sqlalchemy import select

from app.applications.state_machine import ALLOWED
from app.jobs.context import completeness
from app.matching.service import MatchService
from app.models import ApplicationStep, CandidateDocument
from app.services.diagnostics import events, redact, RUN_ID


def snapshot(db, application, manager, full_run=False):
    job = application.job
    diagnostic = dict((job.raw_metadata or {}).get('suitability') or {})
    evaluation = MatchService.evaluation_for(db, application.candidate, job)
    state = diagnostic.get('state') or evaluation.get('decision') or ('WAITING_FOR_JOB_DATA' if (application.review_data or {}).get('job_data_pending') else 'NOT_STARTED')
    if application.status == 'FAILED' and str(((application.review_data or {}).get('failure') or {}).get('stage', '')).startswith(('matching', 'vacancy')):
        state = 'ERROR'
    pages = manager.page_summary()
    current = next((p for p in pages if p['id'] == application.browser_page_id and not p['closed']), None)
    steps = list(db.execute(select(ApplicationStep).where(ApplicationStep.application_id == application.id).order_by(ApplicationStep.sequence)).scalars())
    def step(item):
        return {'action': item.action, 'from': item.state_from, 'to': item.state_to, 'timestamp': str(item.created_at)} if item else None
    cv = db.get(CandidateDocument, application.selected_cv_document_id) if application.selected_cv_document_id else None
    challenge = manager.challenges.get(application.browser_page_id) if hasattr(manager, 'challenges') else None
    result = {'run_id': RUN_ID,
        'browser': {**manager.chrome.payload(), 'connected': bool(manager.browser and manager.browser.is_connected()),
            'status': 'connected' if manager.context else 'disconnected', 'context': 'default persistent context',
            'context_count': len(manager.browser.contexts) if manager.browser else 0, 'page_count': len(pages),
            'active_page_url': current['url'] if current else None,
            'last_navigation': manager.navigation.get(application.browser_page_id),
            'session_id': manager.session_key, 'pages': pages, 'session_state': getattr(manager, 'session_state', {})},
        'vacancy': {'id': job.id, 'source': job.source, 'original_url': job.raw_metadata.get('original_job_url', job.canonical_url),
            'application_url': job.compact_json.get('application_url') or job.raw_metadata.get('external_application_url'),
            'title': job.title, 'company': job.company, 'location': job.location, 'completeness': completeness(job)},
        'suitability': {**diagnostic, 'state': state, 'evaluation': evaluation, 'cv_loaded': bool(cv and application.candidate.data)},
        'pipeline': {'state': application.status, 'previous_state': steps[-1].state_from if steps else None,
            'next_expected_states': sorted(ALLOWED.get(application.status, [])), 'operation': application.current_operation,
            'fallback_level': application.fallback_level, 'retry_count': sum('retry' in s.action or 'resume' in s.action for s in steps),
            'last_successful_step': step(next((s for s in reversed(steps) if s.success), None)),
            'last_failed_step': step(next((s for s in reversed(steps) if not s.success or s.state_to == 'FAILED'), None)),
            'website_unavailable_reason': None if current else 'No live page reference; the backend may have restarted. Resume safely rebinds or opens a page.'},
        'challenge': challenge.payload() if challenge else {'state': 'NONE', 'detected': False},
        'events': events(application.id, job.id, full_run), 'error': application.error}
    return redact(result)

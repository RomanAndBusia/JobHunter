"""Trusted action boundary. A dry run cannot acquire submission authority."""
import re

from app.config import settings
from app.errors import JobHunterError
from app.services.diagnostics import emit


class FinalSubmitBlocked(JobHunterError):
    def __init__(self):
        super().__init__('FINAL_SUBMIT_BLOCKED_BY_TEST_GUARD',
                         'Final submission is disabled for this live test.', 409, 'application.submit')


def require_submission_allowed(application_id=None):
    if settings.live_application_test or not settings.allow_final_submit:
        emit('submit.blocked', application_id=application_id,
             reason='FINAL_SUBMIT_BLOCKED_BY_TEST_GUARD')
        raise FinalSubmitBlocked()


async def guard_action(locator, application_id=None, approved_final=False):
    """Only inspected intermediate actions can execute without approval.

    A form submit defaults to final. A known Next/Continue is intermediate unless
    its accessible context also says send/apply/submit. Unknown form actions fail
    closed instead of assuming that unusual text means Next.
    """
    if approved_final:
        require_submission_allowed(application_id)
    state = await locator.evaluate('''el => ({
      text:el.getAttribute('aria-label') || el.innerText || el.value || '',
      in_form:!!(el.form || el.closest('form')),
      type:el.type || el.getAttribute('type') || '',
      action:el.getAttribute('data-jh-action'),
      final:el.getAttribute('data-final-action') === 'true'
    })''')
    name = re.sub(r'\s+', ' ', state['text']).strip().casefold()
    intermediate = bool(re.fullmatch(r'(next|continue|dalej|kontynuuj|save draft|back|wstecz)(\s*[→›»])?', name))
    if state['type'] == 'button' and re.fullmatch(r'(add|dodaj)\s+[\w -]+|write it here|type it here|write here instead', name):
        intermediate = True
    final_words = bool(re.search(r'\b(submit|send|apply|wyślij|wyslij|aplikuj|finish|complete application)\b', name))
    final = state['final'] or final_words or (state['in_form'] and not intermediate)
    if final:
        require_submission_allowed(application_id)
        if not approved_final:
            raise JobHunterError('submission_approval_required', 'This action requires explicit review approval.', 409, 'application.submit')
    return final

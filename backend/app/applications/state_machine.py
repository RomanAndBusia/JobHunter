from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Application, ApplicationState, ApplicationStep


ALLOWED = {
    'WAITING_FOR_LOGIN': {'OPENING', 'FORM_DISCOVERY', 'PAUSED', 'ABORTED', 'FAILED'},
    ApplicationState.READY.value: {ApplicationState.OPENING.value, ApplicationState.PAUSED.value, ApplicationState.ABORTED.value},
    ApplicationState.PAUSED.value: {ApplicationState.READY.value, ApplicationState.OPENING.value, ApplicationState.FORM_DISCOVERY.value, ApplicationState.ABORTED.value},
    ApplicationState.OPENING.value: {ApplicationState.FORM_DISCOVERY.value, ApplicationState.NEEDS_USER.value, ApplicationState.CAPTCHA.value, ApplicationState.PAUSED.value, ApplicationState.FAILED.value, ApplicationState.ABORTED.value, ApplicationState.EXPIRED.value},
    ApplicationState.FORM_DISCOVERY.value: {ApplicationState.FILLING.value, ApplicationState.UNKNOWN_FORM.value, ApplicationState.NEEDS_USER.value, ApplicationState.CAPTCHA.value, ApplicationState.PAUSED.value, ApplicationState.FAILED.value, ApplicationState.ABORTED.value},
    ApplicationState.FILLING.value: {ApplicationState.VERIFYING.value, ApplicationState.FORM_DISCOVERY.value, ApplicationState.NEEDS_USER.value, ApplicationState.CAPTCHA.value, ApplicationState.PAUSED.value, ApplicationState.FAILED.value, ApplicationState.ABORTED.value},
    ApplicationState.VERIFYING.value: {ApplicationState.FORM_DISCOVERY.value, ApplicationState.REVIEW_REQUIRED.value, ApplicationState.NEEDS_USER.value, ApplicationState.PAUSED.value, ApplicationState.FAILED.value, ApplicationState.ABORTED.value},
    ApplicationState.NEEDS_USER.value: {ApplicationState.FORM_DISCOVERY.value, ApplicationState.FILLING.value, ApplicationState.PAUSED.value, ApplicationState.ABORTED.value, ApplicationState.FAILED.value},
    ApplicationState.CAPTCHA.value: {
        ApplicationState.OPENING.value, ApplicationState.FORM_DISCOVERY.value,
        ApplicationState.FILLING.value, ApplicationState.VERIFYING.value, ApplicationState.SUBMITTING.value,
        ApplicationState.PAUSED.value,
        ApplicationState.ABORTED.value,
    },
    ApplicationState.UNKNOWN_FORM.value: {ApplicationState.FORM_DISCOVERY.value, ApplicationState.NEEDS_USER.value, ApplicationState.PAUSED.value, ApplicationState.FAILED.value, ApplicationState.ABORTED.value},
    ApplicationState.REVIEW_REQUIRED.value: {ApplicationState.APPROVED.value, ApplicationState.FILLING.value, ApplicationState.PAUSED.value, ApplicationState.ABORTED.value},
    ApplicationState.APPROVED.value: {ApplicationState.SUBMITTING.value, ApplicationState.REVIEW_REQUIRED.value, ApplicationState.ABORTED.value},
    ApplicationState.SUBMITTING.value: {ApplicationState.SUBMITTED.value, ApplicationState.REVIEW_REQUIRED.value, ApplicationState.NEEDS_USER.value, ApplicationState.CAPTCHA.value, ApplicationState.FAILED.value},
    ApplicationState.FAILED.value: {ApplicationState.OPENING.value, ApplicationState.FORM_DISCOVERY.value, ApplicationState.ABORTED.value},
    ApplicationState.ABORTED.value: set(),
    ApplicationState.EXPIRED.value: set(),
    ApplicationState.SUBMITTED.value: set(),
}
ALLOWED['OPENING'].add('WAITING_FOR_LOGIN')

RUNNING_STATES = {
    ApplicationState.OPENING.value,
    ApplicationState.FORM_DISCOVERY.value,
    ApplicationState.FILLING.value,
    ApplicationState.VERIFYING.value,
    ApplicationState.APPROVED.value,
    ApplicationState.SUBMITTING.value,
}


def allowed_application_actions(application: Application, page_available: bool = False) -> List[str]:
    """Return the one authoritative action matrix for a persisted application."""
    status = application.status
    if status == ApplicationState.READY.value:
        actions = ["select_cv", "delete"]
        if application.selected_cv_document_id is not None:
            actions[1:1] = ["start", "reset"]
        return actions + (["new-apply"] if application.direct_slot_active else [])
    matrix = {
        ApplicationState.OPENING.value: ["pause", "open-browser", "abort"],
        'WAITING_FOR_LOGIN': ["resume", "pause", "open-browser", "reset", "delete", "abort"],
        ApplicationState.FORM_DISCOVERY.value: ["pause", "open-browser", "abort"],
        ApplicationState.FILLING.value: ["pause", "open-browser", "abort"],
        ApplicationState.VERIFYING.value: ["pause", "open-browser", "abort"],
        ApplicationState.PAUSED.value: ["resume", "reset", "delete", "open-browser", "abort"],
        ApplicationState.NEEDS_USER.value: ["answer", "resume", "manual", "reset", "delete", "open-browser", "abort"],
        ApplicationState.CAPTCHA.value: ["resume", "manual", "reset", "delete", "open-browser", "abort"],
        ApplicationState.UNKNOWN_FORM.value: ["retry", "manual", "reset", "delete", "open-browser", "abort"],
        ApplicationState.FAILED.value: ["retry", "reset", "delete", "open-browser", "abort"],
        ApplicationState.REVIEW_REQUIRED.value: ["review", "submit", "open-browser", "reset", "delete", "abort"],
        ApplicationState.SUBMITTED.value: ["delete"],
        ApplicationState.ABORTED.value: ["reset", "delete"],
        ApplicationState.EXPIRED.value: ["reset", "delete"],
    }
    actions = list(matrix.get(status, ["reset", "delete"]))
    if not page_available and "open-browser" in actions:
        actions.remove("open-browser")
    if application.direct_slot_active and status not in {ApplicationState.APPROVED.value, ApplicationState.SUBMITTING.value}:
        actions.append("new-apply")
    return actions


def transition_application(
    db: Session,
    application: Application,
    new_state: str,
    action: str,
    details: Optional[Dict[str, Any]] = None,
    success: bool = True,
) -> None:
    old_state = application.status
    if new_state != old_state and new_state not in ALLOWED.get(old_state, set()):
        raise ValueError("Invalid application transition %s -> %s" % (old_state, new_state))
    sequence = db.execute(select(func.count(ApplicationStep.id)).where(
        ApplicationStep.application_id == application.id
    )).scalar_one() + 1
    db.add(ApplicationStep(
        application_id=application.id,
        sequence=sequence,
        state_from=old_state,
        state_to=new_state,
        action=action,
        details=details or {},
        success=success,
    ))
    application.status = new_state
    db.flush()
    from app.services.diagnostics import emit
    emit('application.transition', application_id=application.id, vacancy_id=application.job_id,
         previous_state=old_state, state=new_state, action=action, success=success)

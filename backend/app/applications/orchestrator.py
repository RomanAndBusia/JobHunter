import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, Set
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters import GenericAdapter
from app.adapters.justjoin import JustJoinAdapter, LoginPending
from app.applications.state_machine import transition_application
from app.applications.cover_letters import is_cv_field
from app.browser.executor import BrowserActionExecutor
from app.browser.challenge import ChallengeState, ManualVerificationPending
from app.browser.form_scanner import BrowserFormScanner
from app.browser.manager import BrowserSessionLostError, browser_manager
from app.config import settings
from app.db.session import SessionLocal
from app.forms.answer_engine import AnswerEngine, ProposedAnswer, question_concept
from app.forms.fallback import FormFallbackAnalyzer
from app.jobs.page_extractor import GenericJobPageExtractor
from app.jobs.context import acquire, JobDataPending
from app.services.diagnostics import scope, emit
from app.logging import event
from app.matching.service import MatchService
from app.models import (
    Application,
    ApplicationAnswer,
    ApplicationQuestionRequest,
    ApplicationState,
    CandidateDocument,
    FormSnapshot,
    QuestionMemory,
)
from app.schemas.api import SemanticField, SemanticForm
from app.services.events import event_hub
from app.utils.hashing import stable_hash


logger = logging.getLogger(__name__)


def _value_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.translate(str.maketrans({"ł": "l", "Ł": "L"}))
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _browser_value_matches_plan(field: SemanticField, answer: ProposedAnswer) -> bool:
    expected = answer.value
    if isinstance(expected, str):
        option = next((item for item in field.options if item.id == expected), None)
        if option is not None:
            expected = option.label
    return bool(_value_key(expected)) and _value_key(field.value) == _value_key(expected)


def _is_cv_upload(field: SemanticField) -> bool:
    return is_cv_field(field)


@dataclass
class RuntimeForm:
    page_id: str
    form: SemanticForm
    registry: Any
    submit_action_id: Optional[str]
    field_refs: Dict[str, SemanticField] = field(default_factory=dict)


class ApplicationFitReviewRequired(Exception):
    pass


class ApplicationOrchestrator:
    def __init__(self) -> None:
        self.tasks: Dict[int, asyncio.Task] = {}
        self.runtime: Dict[int, RuntimeForm] = {}
        self._locks: Dict[int, asyncio.Lock] = {}
        self._mutation_locks: Dict[int, asyncio.Lock] = {}
        self._deleting: Set[int] = set()
        self._workflow_slots = asyncio.Semaphore(2)

    def _lock(self, application_id: int) -> asyncio.Lock:
        if application_id not in self._locks:
            self._locks[application_id] = asyncio.Lock()
        return self._locks[application_id]

    def mutation_lock(self, application_id: int) -> asyncio.Lock:
        if application_id not in self._mutation_locks:
            self._mutation_locks[application_id] = asyncio.Lock()
        return self._mutation_locks[application_id]

    def forget_application(self, application_id: int) -> None:
        self.tasks.pop(application_id, None)
        self.runtime.pop(application_id, None)
        self._locks.pop(application_id, None)
        self._deleting.discard(application_id)

    def begin_delete(self, application_id: int) -> None:
        """Tombstone an application before task cancellation so no new worker can start."""
        self._deleting.add(application_id)

    def cancel_delete(self, application_id: int) -> None:
        self._deleting.discard(application_id)

    def _task_finished(self, application_id: int, task: asyncio.Task) -> None:
        if self.tasks.get(application_id) is task:
            self.tasks.pop(application_id, None)

    def start(self, application_id: int) -> None:
        if application_id in self._deleting:
            return
        running = self.tasks.get(application_id)
        if running is not None and not running.done():
            return
        task = asyncio.create_task(self._run(application_id))
        self.tasks[application_id] = task
        task.add_done_callback(lambda completed: self._task_finished(application_id, completed))

    def resume(self, application_id: int) -> None:
        if application_id in self._deleting:
            return
        running = self.tasks.get(application_id)
        if running is not None and not running.done():
            return
        task = asyncio.create_task(self._resume(application_id))
        self.tasks[application_id] = task
        task.add_done_callback(lambda completed: self._task_finished(application_id, completed))

    async def release(self, application_id: int, close_page: bool = True) -> bool:
        """Cancel the sole workflow owner and release its borrowed page once."""
        task = self.tasks.get(application_id)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            done, pending = await asyncio.wait({task}, timeout=15.0)
            if pending:
                raise RuntimeError("Application workflow did not stop before deletion")
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                # A completed failed task cannot write again and is safe to release.
                pass
        self.tasks.pop(application_id, None)
        runtime = self.runtime.pop(application_id, None)
        page_id = runtime.page_id if runtime is not None else None
        if page_id is None:
            db = SessionLocal()
            try:
                application = db.get(Application, application_id)
                page_id = application.browser_page_id if application is not None else None
            finally:
                db.close()
        if close_page:
            return await browser_manager.release_application_page(application_id, page_id)
        return False

    async def pause(self, application_id: int) -> None:
        """Stop the workflow task without releasing its persistent application page."""
        await self.release(application_id, close_page=False)

    async def _resume(self, application_id: int) -> None:
        async with self._workflow_slots:
            await self._resume_owned(application_id)

    async def _resume_owned(self, application_id: int) -> None:
        async with self._lock(application_id):
            token = scope.set({"application_id": application_id, "browser_session_id": browser_manager.session_key})
            db = SessionLocal()
            try:
                application = db.get(Application, application_id)
                if application is not None:
                    scope.set({**scope.get(), "vacancy_id": application.job_id})
                if application is None or application.status not in {
                    ApplicationState.PAUSED.value,
                    ApplicationState.NEEDS_USER.value, ApplicationState.CAPTCHA.value,
                    ApplicationState.UNKNOWN_FORM.value, ApplicationState.FAILED.value,
                    'WAITING_FOR_LOGIN',
                }:
                    return
                await browser_manager.resume()
                await browser_manager.ensure_started()
                page = None
                if application.browser_page_id:
                    page = await browser_manager.resolve_active_application_page(
                        application.id, application.browser_page_id, application.target_url)
                    application.browser_page_id = browser_manager._register(page)
                    db.commit()
                event(logger, 'application.resume_after_answers', application_id=application.id,
                      page_id=application.browser_page_id, state=application.status)
                application.error = None
                if (application.review_data or {}).get('fit_review_required') or (application.review_data or {}).get('job_data_pending'):
                    target = await self._open(db, application, loaded_job_page=page is not None)
                    application.browser_page_id = target.page_id
                    application.target_type = target.target_type
                    application.target_host = target.target_host
                    application.target_url = target.target_url
                    self._transition(db, application, ApplicationState.FORM_DISCOVERY.value, 'fit_review_resolved')
                    await self._process_steps(db, application, target.page)
                    return
                paused_from = (application.review_data or {}).get("paused_from")
                if application.status == 'WAITING_FOR_LOGIN' or application.status == ApplicationState.PAUSED.value and paused_from in {ApplicationState.OPENING.value, 'WAITING_FOR_LOGIN'}:
                    self._transition(db, application, ApplicationState.OPENING.value, "resume_opening_step")
                    target = await self._open(db, application, loaded_job_page=page is not None)
                    application.browser_page_id = target.page_id
                    application.target_type = target.target_type
                    application.target_host = target.target_host
                    application.target_url = target.target_url
                    self._transition(db, application, ApplicationState.FORM_DISCOVERY.value, "target_detected_after_pause")
                    await self._process_steps(db, application, target.page)
                    return
                if application.status == ApplicationState.CAPTCHA.value and not application.target_type:
                    self._transition(db, application, ApplicationState.OPENING.value, "resume_manual_verification")
                    target = await self._open(db, application, loaded_job_page=page is not None)
                    application.browser_page_id = target.page_id
                    application.target_type = target.target_type
                    application.target_host = target.target_host
                    application.target_url = target.target_url
                    self._transition(db, application, ApplicationState.FORM_DISCOVERY.value, "target_detected_after_verification")
                    await self._process_steps(db, application, target.page)
                    return
                if page is None:
                    target = await self._open(db, application, loaded_job_page=False)
                    application.browser_page_id = target.page_id
                    application.target_type = target.target_type
                    application.target_host = target.target_host
                    application.target_url = target.target_url
                    self._transition(db, application, ApplicationState.FORM_DISCOVERY.value, "target_reopened_after_session_loss")
                    await self._process_steps(db, application, target.page)
                    return
                self._transition(db, application, ApplicationState.FORM_DISCOVERY.value, "resume_after_manual_action")
                await self._process_steps(db, application, page)
            except ApplicationFitReviewRequired as exc:
                application = db.get(Application, application_id)
                await self._handle_fit_review(db, application, exc)
                return
            except JobDataPending as exc:
                application.review_data = {**(application.review_data or {}), 'job_data_pending': True}
                application.current_operation = 'Waiting for vacancy details before suitability analysis'
                application.error = exc.safe_message
                self._transition(db, application, ApplicationState.PAUSED.value, 'waiting_for_job_data')
                return
            except (ManualVerificationPending, LoginPending):
                return
            except Exception as exc:
                db.rollback()
                application = db.get(Application, application_id)
                if application is not None:
                    stage = getattr(exc, "stage", "application.form")
                    if stage.startswith("matching"):
                        application.current_operation = "Suitability evaluation failed"
                    application.error = getattr(exc, "safe_message", str(exc))
                    application.review_data = {
                        **(application.review_data or {}),
                        "failure": {"type": getattr(exc, "error_type", type(exc).__name__), "stage": stage},
                    }
                    try:
                        self._transition(db, application, ApplicationState.FAILED.value, "resume_failed")
                    except ValueError:
                        db.rollback()
                logger.exception(
                    "Application resume failed",
                    extra={"context": {
                        "application_id": application_id,
                        "module": "app.applications.orchestrator",
                        "stage": "application.form",
                        "exception_class": type(exc).__name__,
                    }},
                )
            finally:
                db.close()
                scope.reset(token)

    def _publish(self, application: Application) -> None:
        event_hub.publish("applications", {
            "application_id": application.id,
            "status": application.status,
            "step": application.current_step,
            "operation": application.current_operation,
            "last_operation": application.last_operation,
            "fallback_level": application.fallback_level,
            "error": application.error,
        })

    def _transition(self, db: Session, application: Application, state: str, action: str, details: Optional[Dict[str, Any]] = None) -> None:
        transition_application(db, application, state, action, details)
        db.commit()
        self._publish(application)
        event(logger, "application_transition", application_id=application.id, job_id=application.job_id, step=application.current_step, action=action, result=state)

    def _challenge_callback(self, db: Session, application: Application, resume_state: str):
        async def update(stage: str, state: ChallengeState) -> None:
            db.refresh(application)
            if application.status in {
                ApplicationState.ABORTED.value, ApplicationState.SUBMITTED.value,
                ApplicationState.EXPIRED.value,
            }:
                return
            if stage == "waiting":
                if state.page_id:
                    if state.page_id in browser_manager.pages:
                        await browser_manager.bind_application_page(application.id, browser_manager.pages[state.page_id])
                    application.browser_page_id = state.page_id
                application.target_url = state.url
                application.target_host = urlsplit(state.url).netloc
                application.current_operation = "Waiting for manual verification..."
                application.error = "Complete the verification in the visible browser; JobHunter will resume automatically."
                self._transition(
                    db, application, ApplicationState.CAPTCHA.value, "manual_verification_waiting",
                    {"provider": state.provider, "evidence": state.evidence, "url": state.url},
                )
            elif stage == "completed":
                application.current_operation = "Verification completed"
                application.last_operation = "Verification completed"
                application.error = None
                db.commit()
                self._publish(application)
            elif stage == "continuing":
                application.current_operation = "Continuing application..."
                self._transition(db, application, resume_state, "manual_verification_completed")
            elif stage == "timeout":
                application.current_operation = "Waiting for manual verification..."
                application.error = "Manual verification is still required. Complete it in the visible browser, then use Resume."
                db.commit()
                self._publish(application)
        return update

    async def _wait_for_challenge(
        self,
        db: Session,
        application: Application,
        page: Any,
        resume_state: str,
    ) -> bool:
        result = await browser_manager.wait_for_manual_verification(
            page, self._challenge_callback(db, application, resume_state),
            include_pending=resume_state in {ApplicationState.VERIFYING.value, ApplicationState.SUBMITTING.value},
        )
        return result.cleared

    async def _handle_fit_review(self, db, application, exc):
        from app.applications.policy import explicit_direct_apply
        if not explicit_direct_apply(application):
            decision = ((application.review_data or {}).get('evaluation') or {}).get('decision')
            application.current_operation = 'Suitability: ' + str(decision)
            application.error = str(exc)
            self._transition(db, application, ApplicationState.ABORTED.value if decision == 'SKIP'
                             else ApplicationState.NEEDS_USER.value, 'fit_review_required')
            return
        # Compatibility with a persisted fit-review marker or an older caller:
        # explicit intent still proceeds without forcing a different evaluation.
        application.review_data = {**(application.review_data or {}), 'fit_review_required': False,
                                   'suitability_advisory': True}
        application.error = None
        page = await browser_manager.resolve_active_application_page(
            application.id, application.browser_page_id, application.target_url)
        adapter = self._adapter(db, application)
        target = await adapter.open_loaded_application(
            browser_manager, page, application.job,
            self._challenge_callback(db, application, ApplicationState.OPENING.value))
        application.browser_page_id = await browser_manager.bind_application_page(application.id, target.page)
        application.target_type, application.target_host, application.target_url = target.target_type, target.target_host, target.target_url
        self._transition(db, application, ApplicationState.FORM_DISCOVERY.value, 'suitability_advisory')
        await self._process_steps(db, application, target.page)

    def _adapter(self, db, application):
        async def login(stage, page):
            application.browser_page_id = await browser_manager.bind_application_page(application.id, page)
            application.target_url = page.url
            application.target_host = urlsplit(page.url).netloc
            if stage == 'waiting':
                application.current_operation = 'Waiting for Just Join IT login'
                self._transition(db, application, 'WAITING_FOR_LOGIN', 'justjoin.login.waiting')
            else:
                application.current_operation = 'Login complete; continuing application'
                self._transition(db, application, 'OPENING', 'justjoin.login.success')
        return JustJoinAdapter(login_callback=login) if JustJoinAdapter().matches(application.job.canonical_url) else GenericAdapter()

    async def _open(self, db: Session, application: Application, loaded_job_page: bool = False) -> Any:
        from app.applications.policy import explicit_direct_apply
        advisory = explicit_direct_apply(application)
        await browser_manager.ensure_started()
        page = (browser_manager.get_page(application.browser_page_id)
                if loaded_job_page and application.browser_page_id
                else await browser_manager.new_application_page())
        application.browser_page_id = browser_manager._register(page)
        await browser_manager.bind_application_page(application.id, page)
        db.commit()
        adapter = self._adapter(db, application)
        callback = self._challenge_callback(db, application, ApplicationState.OPENING.value)
        if not loaded_job_page:
            application.current_operation = "Opening vacancy page"
            db.commit()
            self._publish(application)
            await adapter.navigate_job_page(browser_manager, page, application.job, callback)
        elif not await self._wait_for_challenge(db, application, page, ApplicationState.OPENING.value):
            raise ManualVerificationPending("Manual verification is still required")
        application.target_url = page.url
        application.target_host = urlsplit(page.url).netloc
        await browser_manager.bind_application_page(application.id, page)
        application.current_operation = "Acquiring vacancy details before suitability"
        db.commit()
        self._publish(application)
        try:
            await acquire(db, application.job, page, browser_manager, callback,
                          application_id=application.id, require_complete=not advisory)
        except JobDataPending:
            if not advisory:
                raise
        page = await browser_manager.resolve_active_application_page(application.id)
        application.browser_page_id = browser_manager._register(page)
        application.target_url = page.url
        application.target_host = urlsplit(page.url).netloc
        application.review_data = {**(application.review_data or {}), 'job_data_pending': False}
        application.current_operation = "Evaluating vacancy suitability"
        db.commit()
        self._publish(application)
        advisory_error = None
        try:
            await MatchService(db, application_id=application.id).run(application.candidate, [application.job.id])
        except Exception as exc:
            if not advisory:
                raise
            db.rollback()
            advisory_error = getattr(exc, 'error_type', type(exc).__name__)
            event(logger, 'direct_apply.suitability_unavailable', application_id=application.id,
                  error_type=advisory_error, stage=getattr(exc, 'stage', 'matching.inference'))
        evaluation = MatchService.evaluation_for(db, application.candidate, application.job)
        application.review_data = {**(application.review_data or {}), 'evaluation': evaluation}
        db.commit()
        if advisory:
            application.review_data = {**application.review_data, 'suitability_advisory': True,
                                       'suitability_error': advisory_error, 'fit_review_required': False}
            db.commit()
            event(logger, 'direct_apply.suitability_advisory', application_id=application.id,
                  vacancy_id=application.job_id, decision=(evaluation or {}).get('decision'),
                  advisory=True, error_type=advisory_error)
            target = await adapter.open_loaded_application(browser_manager, page, application.job, callback)
            await browser_manager.bind_application_page(application.id, target.page)
            return target
        if application.job.matching_status in {'AI_ERROR', 'AI_REQUIRED'} or not evaluation:
            from app.errors import JobHunterError
            raise JobHunterError('suitability_unavailable', 'Suitability evaluation failed; inspect Log for the inference/schema/persistence stage.', 502, 'matching.inference')
        if evaluation.get('decision') != 'APPLY':
            application.review_data = {**application.review_data, 'fit_review_required': True}
            db.commit()
            raise ApplicationFitReviewRequired('Suitability: %s. %s' % (evaluation.get('decision'), '; '.join(evaluation.get('reasons', []))))
        application.review_data = {**application.review_data, 'fit_review_required': False}
        db.commit()
        target = await adapter.open_loaded_application(browser_manager, page, application.job, callback)
        await browser_manager.bind_application_page(application.id, target.page)
        return target

    @staticmethod
    def _action_for(field: SemanticField, answer: ProposedAnswer) -> str:
        if answer.action in {"fill", "select_option", "check", "uncheck", "upload"}:
            return answer.action
        if field.control_kind in {"NATIVE_SELECT", "CUSTOM_SELECT", "COMBOBOX", "AUTOCOMPLETE", "RADIO_GROUP", "RATING"}:
            return "choose"
        if field.control_kind in {"CHECKBOX", "CHECKBOX_GROUP", "SWITCH", "CONSENT"}:
            return "check" if bool(answer.value) else "uncheck"
        if field.type == "file":
            return "upload"
        return "fill"

    @staticmethod
    def _review(db: Session, application: Application) -> Dict[str, Any]:
        answers = list(db.execute(select(ApplicationAnswer).where(
            ApplicationAnswer.application_id == application.id
        ).order_by(ApplicationAnswer.id)).scalars())
        return {
            "origin": (application.review_data or {}).get('origin', application.job.source),
            "suitability_advisory": (application.review_data or {}).get('suitability_advisory', False),
            "suitability_error": (application.review_data or {}).get('suitability_error'),
            "diagnostic_mode": bool((application.review_data or {}).get('diagnostic_mode')),
            "live_application_test": bool((application.review_data or {}).get('live_application_test')),
            "evaluation": (application.review_data or {}).get("evaluation"),
            "cover_letter": (application.review_data or {}).get("cover_letter"),
            "job": {"title": application.job.title, "company": application.job.company, "url": application.job.canonical_url},
            "source": application.job.source,
            "provider": (application.job.raw_metadata or {}).get("provider"),
            "ai_runtime": settings.ai_runtime,
            "target": {"type": application.target_type, "host": application.target_host, "url": application.target_url},
            "documents": list(application.document_versions or []),
            "answers": [{
                "id": answer.id, "field_id": answer.field_id, "question": answer.question,
                "value": answer.value, "source": answer.source, "verified": answer.verified,
                "manual_override": answer.manual_override, "meaning": answer.semantic_meaning,
                "source_paths": answer.source_paths, "confidence": answer.confidence,
                "execution_confidence": answer.execution_confidence,
                "reason": answer.reason, "status": answer.status,
                "synthetic_test": answer.source.startswith('SYNTHETIC_TEST'),
            } for answer in answers],
        }

    async def _document_for(self, db: Session, application: Application) -> Optional[Dict[str, Any]]:
        if not application.selected_cv_document_id:
            return None
        document = db.execute(select(CandidateDocument).where(
            CandidateDocument.id == application.selected_cv_document_id,
            CandidateDocument.profile_id == application.candidate_id,
            CandidateDocument.kind == "CV",
        )).scalar_one_or_none()
        if document is None:
            return None
        return {
            "id": document.id, "kind": document.kind, "version": document.version,
            "name": document.original_name, "path": document.storage_path,
            "sha256": document.sha256,
            "extracted_text": document.extracted_text,
        }

    async def _run(self, application_id: int) -> None:
        async with self._workflow_slots:
            await self._run_owned(application_id)

    async def _run_owned(self, application_id: int) -> None:
        async with self._lock(application_id):
            token = scope.set({"application_id": application_id, "browser_session_id": browser_manager.session_key})
            db = SessionLocal()
            try:
                application = db.get(Application, application_id)
                if application is not None:
                    scope.set({**scope.get(), "vacancy_id": application.job_id})
                if application is None or application.status == ApplicationState.SUBMITTED.value:
                    return
                if application.status not in {
                    ApplicationState.READY.value, ApplicationState.FAILED.value,
                    ApplicationState.OPENING.value,
                }:
                    return
                if application.selected_cv_document_id is None:
                    raise ValueError("Choose a CV before starting this application")
                application.started_at = application.started_at or datetime.utcnow()
                if settings.live_application_test:
                    application.review_data = {**(application.review_data or {}), 'live_application_test': True}
                application.current_operation = "Opening job and application target"
                if application.status != ApplicationState.OPENING.value:
                    self._transition(db, application, ApplicationState.OPENING.value, "open_application")
                else:
                    db.commit()
                    self._publish(application)
                target = await self._open(db, application)
                application.browser_page_id = target.page_id
                application.target_type = target.target_type
                application.target_host = target.target_host
                application.target_url = target.target_url
                application.navigation_history = list(application.navigation_history or []) + [{"url": target.target_url, "at": datetime.utcnow().isoformat()}]
                application.current_operation = "Discovering form"
                self._transition(db, application, ApplicationState.FORM_DISCOVERY.value, "target_detected", {"type": target.target_type, "url": target.target_url})
                await self._process_steps(db, application, target.page)
            except ApplicationFitReviewRequired as exc:
                application = db.get(Application, application_id)
                await self._handle_fit_review(db, application, exc)
                return
            except JobDataPending as exc:
                application.review_data = {**(application.review_data or {}), 'job_data_pending': True}
                application.current_operation = 'Waiting for vacancy details before suitability analysis'
                application.error = exc.safe_message
                self._transition(db, application, ApplicationState.PAUSED.value, 'waiting_for_job_data')
                return
            except (ManualVerificationPending, LoginPending):
                application = db.get(Application, application_id)
                if application is not None:
                    db.commit()
                    self._publish(application)
                return
            except Exception as exc:
                db.rollback()
                application = db.get(Application, application_id)
                if application is not None and application.status not in {ApplicationState.SUBMITTED.value, ApplicationState.ABORTED.value}:
                    stage = getattr(exc, "stage", "application.form")
                    if stage.startswith("matching"):
                        application.current_operation = "Suitability evaluation failed"
                    application.error = getattr(exc, "safe_message", str(exc))
                    application.review_data = {
                        **(application.review_data or {}),
                        "failure": {"type": getattr(exc, "error_type", type(exc).__name__), "stage": stage},
                    }
                    try:
                        self._transition(db, application, ApplicationState.FAILED.value, "orchestrator_error", {"error_type": type(exc).__name__})
                    except ValueError:
                        db.rollback()
                        application.status = ApplicationState.FAILED.value
                        db.commit()
                        self._publish(application)
                logger.exception(
                    "Application workflow failed",
                    extra={"context": {
                        "application_id": application_id,
                        "module": "app.applications.orchestrator",
                        "stage": getattr(exc, "stage", "application.form"),
                        "runtime": settings.ai_runtime,
                        "model": settings.ai_model,
                        "request_id": getattr(exc, "details", {}).get("request_id") if hasattr(exc, "details") else None,
                        "exception_class": type(exc).__name__,
                    }},
                )
            finally:
                db.close()
                scope.reset(token)

    async def _process_steps(self, db: Session, application: Application, page: Any) -> None:
        from app.forms.identity import field_key, structure
        scanner = BrowserFormScanner()
        answer_engine = AnswerEngine(db, application_id=application.id)
        all_runtime_refs = {}
        step_index = int(application.current_step or '1')
        for _scan_attempt in range(32):
            await browser_manager.wait_until_resumed()
            page = await browser_manager.resolve_active_application_page(
                application.id, application.browser_page_id, application.target_url)
            application.browser_page_id = browser_manager._register(page)
            application.current_step = str(step_index)
            application.target_url = page.url
            application.target_host = urlsplit(page.url).netloc.casefold()
            if not await self._wait_for_challenge(
                db, application, page, ApplicationState.FORM_DISCOVERY.value,
            ):
                return
            form, registry = await scanner.scan(page)
            reveal = next((action for action in form.actions if action.get("type") == "reveal_textarea"), None)
            if reveal is not None:
                reveal_result = await BrowserActionExecutor(page, registry).click_action(
                    reveal["id"], expected_change=True,
                )
                if reveal_result.success:
                    form, registry = await scanner.scan(page)
            db.add(FormSnapshot(
                application_id=application.id, url=page.url, host=application.target_host,
                step=application.current_step, semantic_form=form.model_dump(),
                validation_errors=form.validation_errors, fallback_level=application.fallback_level,
                fingerprint=form.fingerprint,
            ))
            db.commit()
            if not form.fields:
                analyzer = FormFallbackAnalyzer(answer_engine.ai)
                fallback = await analyzer.analyze(page, form)
                screenshot = await browser_manager.capture_diagnostic(page, "application-%d-unknown-form" % application.id)
                application.fallback_level = fallback.level
                application.error = fallback.analysis.get("reason", "No interactive form fields found")
                self._transition(db, application, ApplicationState.UNKNOWN_FORM.value, "fallback_exhausted", {"level": fallback.level, "input_chars": fallback.input_chars, "screenshot": screenshot})
                return
            application.current_operation = "Preparing truthful answers"
            self._transition(db, application, ApplicationState.FILLING.value, "form_discovered", {"field_count": len(form.fields), "fingerprint": form.fingerprint})
            executor = BrowserActionExecutor(page, registry, application.id, await self._document_for(db, application))
            dynamic_kinds = {"CUSTOM_SELECT", "COMBOBOX", "AUTOCOMPLETE", "MULTI_SELECT"}
            for field_index, dynamic_field in enumerate(list(form.fields)):
                if dynamic_field.control_kind in dynamic_kinds and not dynamic_field.options:
                    try:
                        discovered = await executor.discover_options(dynamic_field)
                    except Exception as exc:
                        event(
                            logger, "form.control_options_discovery_failed",
                            application_id=application.id, field_id=dynamic_field.id,
                            control_kind=dynamic_field.control_kind, error_type=type(exc).__name__,
                        )
                        continue
                    if discovered.options:
                        form.fields[field_index] = discovered
            needs_user = []
            from app.applications.cover_letters import CoverLetterService, is_letter_field, letter_field_mode
            if settings.cover_letter_mode != 'off' and any(letter_field_mode(field) for field in form.fields):
                application.current_operation = 'Generating cover letter'
                db.commit()
                self._publish(application)
            letter = await CoverLetterService().prepare(application, form.fields, getattr(answer_engine, 'ai', None))
            db.commit()
            if letter and letter['answer'].action == 'upload':
                executor.document_bindings[letter['field'].id] = letter['document']
            plan = await answer_engine.plan(application.candidate, application.job, form.fields, form_actions=form.actions)
            from app.forms.language_collection import plan_collections
            collection_overrides, collection_commits = await plan_collections(
                answer_engine, application.candidate, form.fields, executor)
            for index, field in enumerate(form.fields):
                if field.id in collection_overrides:
                    plan[index] = collection_overrides[field.id]
                if letter and field.id == letter['field'].id:
                    plan[index] = letter['answer']
                elif is_letter_field(field):
                    plan[index] = ProposedAnswer(field.id, field.question, None, 'NO_COVER_LETTER', 1.0,
                                                  needs_user=field.required, reason='Cover letter is disabled, unsupported or lacks explicit evidence')
            group_to_expand = next((g for g in answer_engine.repeat_groups if g.current_rows < g.desired_rows), None)
            if group_to_expand:
                expanded = await executor.click_action(group_to_expand.add_action_id, expected_change=True)
                if expanded.success:
                    await browser_manager.bind_application_page(application.id, page)
                    self._transition(db, application, ApplicationState.FORM_DISCOVERY.value, 'repeat_group_expanded')
                    continue
            rescan_required = False
            for field, answer in zip(form.fields, plan):
                await browser_manager.wait_until_resumed()
                current_page = await browser_manager.resolve_active_application_page(application.id)
                if current_page is not page:
                    application.browser_page_id = browser_manager._register(current_page)
                    rescan_required = True
                    break
                global_field_id = "s%d:%s" % (step_index, field_key(field))
                if field.metadata.get('compound_id') and answer.requested_fact_path:
                    global_field_id = 's%d:k%s' % (step_index, stable_hash([field_key(field), answer.requested_fact_path])[:24])
                if field.control_kind in dynamic_kinds and not field.options:
                    try:
                        discovered = await executor.discover_options(field, str(answer.value or ""))
                    except Exception as exc:
                        event(logger, 'control.options_failed', application_id=application.id,
                              field_id=field.id, error_type=type(exc).__name__)
                        discovered = field
                    if discovered.options:
                        field = discovered
                        answer = (await answer_engine.plan(
                            application.candidate, application.job, [field],
                            {"reason": "dynamic_options_discovered", "actual_options": [item.model_dump() for item in field.options]},
                        ))[0]
                all_runtime_refs[global_field_id] = field
                if answer.source == 'COLLECTION' and answer.action == 'skip':
                    continue
                value = answer.value
                if field.type == "file" and _is_cv_upload(field):
                    document = await self._document_for(db, application)
                    if document:
                        value = document["path"]
                        answer.source = "DOCUMENT"
                        answer.needs_user = False
                        answer.action = 'upload'
                        answer.confidence = 1.0
                        answer.execution_confidence = 'EXACT'
                        answer.reason = 'Trusted candidate-owned pinned CV; uploaded format and hash are recorded in the document manifest.'
                        known_documents = list(application.document_versions or [])
                        if not any(item.get("id") == document["id"] for item in known_documents):
                            known_documents.append({key: document[key] for key in ("id", "kind", "version", "name", "sha256")})
                            application.document_versions = known_documents
                elif field.type == 'file' and field.id in executor.document_bindings:
                    value = executor.document_bindings[field.id]['path']
                    answer.action = 'upload'
                    answer.needs_user = False
                    binding = executor.document_bindings[field.id]
                    manifest = {'id': 'cover-letter:%d' % application.id, 'kind': 'COVER_LETTER', 'version': 1,
                                'name': binding['name'], 'sha256': binding['sha256']}
                    application.document_versions = [item for item in (application.document_versions or []) if item.get('kind') != 'COVER_LETTER'] + [manifest]
                elif field.type == 'file':
                    # No model-generated path can authorize a local file. Other document
                    # kinds require an explicitly selected/generated document binding.
                    value = None
                    answer.needs_user = field.required
                    if field.value:
                        # A manual attachment is verified in the live input;
                        # it never grants the model filesystem access.
                        stale = db.execute(select(ApplicationQuestionRequest).where(
                            ApplicationQuestionRequest.application_id == application.id,
                            ApplicationQuestionRequest.field_id == global_field_id,
                            ApplicationQuestionRequest.status == 'WAITING_FOR_USER',
                        )).scalar_one_or_none()
                        if stale:
                            stale.status = 'RESOLVED'
                            db.commit()
                        continue
                if answer.needs_user or value is None:
                    if field.required or answer.needs_user:
                        needs_user.append({
                            "field_id": global_field_id, "question": (answer.question if field.metadata.get('compound_id') else field.question),
                            "type": field.type, "meaning": answer.meaning, "reason": answer.reason,
                            "fact_path": answer.requested_fact_path, "source_paths": answer.source_paths,
                            "question_category": answer.question_category,
                            "suggested_value": answer.suggested_value,
                            "suggestion_reason": answer.suggestion_reason,
                            "local_field_id": field.id, "required": field.required,
                            "multiple": field.control_kind in {'CHECKBOX_GROUP', 'MULTI_SELECT'},
                            "page": page.url, "step": application.current_step,
                        })
                    continue
                stale_questions = db.execute(select(ApplicationQuestionRequest).where(
                    ApplicationQuestionRequest.application_id == application.id,
                    ApplicationQuestionRequest.field_id == global_field_id,
                    ApplicationQuestionRequest.status == 'WAITING_FOR_USER',
                )).scalars()
                for stale in stale_questions:
                    stale.status = 'RESOLVED'
                existing = db.execute(select(ApplicationAnswer).where(
                    ApplicationAnswer.application_id == application.id,
                    ApplicationAnswer.field_id == global_field_id,
                )).scalar_one_or_none()
                if (
                    field.value not in (None, "", False)
                    and field.type != "file"
                    and ((existing is not None and existing.manual_override) or _browser_value_matches_plan(field, answer))
                ):
                    if existing is None:
                        existing = ApplicationAnswer(
                            application_id=application.id, field_id=global_field_id,
                            question=field.question, value=field.value, source=answer.source,
                            verified=True, semantic_meaning=answer.meaning,
                            source_paths=answer.source_paths, confidence=answer.confidence,
                            execution_confidence=answer.execution_confidence,
                            reason="The existing browser value matches the grounded form plan.",
                            status="VERIFIED",
                        )
                        db.add(existing)
                    elif existing.manual_override:
                        existing.value = field.value
                        existing.source = "MANUAL_BROWSER_VALUE"
                        existing.verified = True
                        existing.status = "VERIFIED"
                    elif answer.source.startswith('SYNTHETIC_TEST'):
                        # A confirmed test answer can equal an earlier live value.
                        # Reuse verified state while retaining its test provenance.
                        existing.value = field.value
                        existing.source = answer.source
                        existing.source_paths = answer.source_paths
                        existing.verified = True
                        existing.status = "VERIFIED"
                    db.commit()
                    continue
                application.current_operation = "Filling %s" % field.question[:120]
                action = self._action_for(field, answer)
                result = await executor.execute(action, field, value)
                if not result.success and not field.metadata.get('compound_id'):
                    browser_manager.ensure_page(page)
                    fresh_form, fresh_registry = await scanner.scan(page)
                    fresh_field = next((item for item in fresh_form.fields if item.question == field.question and item.type == field.type), None)
                    if fresh_field is not None:
                        event(logger, "ai.form_repair", application_id=application.id, field_id=global_field_id, error=result.error)
                        repair = (await answer_engine.plan(
                            application.candidate, application.job, [fresh_field],
                            {
                                "reason": "field_validation_failed", "previous_answer": value,
                                "actual_value": result.actual_value, "error": result.error,
                                "validation_errors": fresh_form.validation_errors,
                            },
                        ))[0]
                        if not repair.needs_user and repair.value is not None:
                            repair_executor = BrowserActionExecutor(page, fresh_registry, application.id, await self._document_for(db, application), executor.document_bindings)
                            repair_result = await repair_executor.execute(self._action_for(fresh_field, repair), fresh_field, repair.value)
                            if repair_result.success:
                                field, answer, value, result = fresh_field, repair, repair.value, repair_result
                                executor.selected_document = repair_executor.selected_document
                if result.success and field.type == 'file' and _is_cv_upload(field) and executor.selected_document:
                    uploaded = executor.selected_document
                    value = result.actual_value[0]
                    manifest = {key:uploaded[key] for key in ('id','kind','version','name','sha256')}
                    manifest['name'] = value
                    if value != uploaded['name']:
                        manifest.update(source_name=uploaded['name'], source_sha256=uploaded['sha256'])
                    manifest.update({key:uploaded[key] for key in ('source_name','source_sha256','conversion') if key in uploaded})
                    application.document_versions = [item for item in (application.document_versions or []) if item.get('id') != uploaded['id']] + [manifest]
                elif result.success and field.control_kind in {'CHECKBOX', 'SWITCH', 'CONSENT'}:
                    value = result.actual_value
                if existing is None:
                    existing = ApplicationAnswer(
                        application_id=application.id, field_id=global_field_id,
                        question=field.question, value=value, source=answer.source,
                        semantic_meaning=answer.meaning, source_paths=answer.source_paths,
                        confidence=answer.confidence, reason=answer.reason,
                        execution_confidence=answer.execution_confidence,
                    )
                    db.add(existing)
                existing.value = value
                existing.source = answer.source
                existing.verified = result.success
                existing.semantic_meaning = answer.meaning
                existing.source_paths = answer.source_paths
                existing.confidence = answer.confidence
                existing.execution_confidence = answer.execution_confidence
                existing.reason = answer.reason
                existing.status = "VERIFIED" if result.success else "FAILED_VALIDATION"
                application.last_operation = application.current_operation
                db.commit()
                if not result.success:
                    analyzer = FormFallbackAnalyzer(answer_engine.ai)
                    fallback = await analyzer.analyze(page, form, field.id)
                    screenshot = await browser_manager.capture_diagnostic(page, "application-%d-field-failure" % application.id)
                    application.fallback_level = fallback.level
                    application.error = result.error
                    needs_user.append({"field_id": global_field_id, "local_field_id": field.id, "question": field.question, "error": result.error, "fallback": fallback.analysis, "screenshot": screenshot})
                # Stop consuming a plan as soon as its live structure/options
                # change. The next pass plans from a fresh DOM and registry.
                await browser_manager.bind_application_page(application.id, page)
                changed_form, _ = await scanner.scan(page)
                if structure(changed_form) != structure(form):
                    event(logger, 'form.dynamic_rescan', application_id=application.id,
                          field_key=field_key(field), step=step_index)
                    rescan_required = True
                    break
            if rescan_required:
                self._transition(db, application, ApplicationState.FORM_DISCOVERY.value, 'dependent_fields_discovered')
                continue
            for collection in collection_commits.values():
                if any(item.get('local_field_id') in {collection[0].id, collection[2].id} for item in needs_user):
                    continue
                result = await executor.commit_language_collection(*collection)
                if result.success:
                    await browser_manager.bind_application_page(application.id, page)
                    event(logger, 'form.dynamic_rescan', application_id=application.id,
                          reason='language_collection_added', step=step_index)
                    rescan_required = True
                else:
                    level_field = collection[2]
                    needs_user.append({'field_id': 's%d:%s' % (step_index, field_key(level_field)),
                                       'local_field_id': level_field.id, 'question': level_field.question,
                                       'type': level_field.type, 'reason': result.error})
            if rescan_required:
                self._transition(db, application, ApplicationState.FORM_DISCOVERY.value, 'language_collection_added')
                continue
            fresh_form, _fresh_registry = await scanner.scan(page)
            if structure(fresh_form) != structure(form):
                self._transition(db, application, ApplicationState.FORM_DISCOVERY.value, 'dependent_fields_discovered')
                continue
            if needs_user:
                for request_data in needs_user:
                    local_field_id = request_data.get('local_field_id')
                    field = next((item for item in form.fields if item.id == local_field_id), None)
                    # Broad preference paths can represent different units/contracts.
                    # They are not sufficient evidence that two questions are equivalent.
                    if request_data.get('fact_path') and request_data['fact_path'] != 'preferences.salary_expectations':
                        same_fact = db.execute(select(ApplicationQuestionRequest).where(
                            ApplicationQuestionRequest.application_id == application.id,
                            ApplicationQuestionRequest.fact_path == request_data['fact_path'],
                            ApplicationQuestionRequest.status == 'WAITING_FOR_USER',
                        )).scalars().first()
                        if (same_fact is not None and same_fact.field_id != request_data['field_id']
                                and field is not None and same_fact.answer_type == field.type
                                and [o.get('label') for o in same_fact.options] == [o.label for o in field.options]):
                            continue
                    request = db.execute(select(ApplicationQuestionRequest).where(
                        ApplicationQuestionRequest.application_id == application.id,
                        ApplicationQuestionRequest.field_id == request_data["field_id"],
                    )).scalar_one_or_none()
                    if request is None:
                        request = ApplicationQuestionRequest(
                            application_id=application.id, candidate_id=application.candidate_id,
                            field_id=request_data["field_id"], question=request_data["question"],
                            concept=question_concept(request_data["question"]),
                            fact_path=request_data.get("fact_path"),
                            answer_type=(field.type if field else request_data.get("type", "text")),
                            options=([item.model_dump() for item in field.options] if field else []),
                            field_context={'required': field.required if field else True,
                                           'multiple': bool(field and field.control_kind in {'CHECKBOX_GROUP', 'MULTI_SELECT'}),
                                           'page': page.url, 'step': application.current_step,
                                           'semantic_key': field_key(field) if field else None},
                            question_category=request_data.get("question_category", "FACTUAL"),
                            suggested_value=request_data.get("suggested_value"),
                            suggestion_reason=request_data.get("suggestion_reason"),
                            scope='APPLICATION_ONLY',
                            status="WAITING_FOR_USER",
                        )
                        db.add(request)
                        db.flush()
                        event(logger, 'application.question_created', application_id=application.id,
                              question_id=request.id, category=request.question_category)
                    elif field is not None:
                        request.question = request_data['question']
                        request.options = [item.model_dump() for item in field.options]
                        request.answer_type = field.type
                        request.status = 'WAITING_FOR_USER'
                        request.field_context = {**(request.field_context or {}), 'required': field.required,
                                                 'multiple': field.control_kind in {'CHECKBOX_GROUP', 'MULTI_SELECT'},
                                                 'page': page.url, 'step': application.current_step,
                                                 'semantic_key': field_key(field)}
                db.commit()
                event(logger, 'application.questions_created', application_id=application.id,
                      count=len(needs_user), step=step_index)
                application.review_data = {"needs_user": needs_user, **self._review(db, application)}
                pending_count = db.query(ApplicationQuestionRequest).filter_by(
                    application_id=application.id, status='WAITING_FOR_USER').count()
                application.error = "%d answer%s %s needed before JobHunter can continue." % (
                    pending_count, "" if pending_count == 1 else "s",
                    "is" if pending_count == 1 else "are",
                )
                self.runtime[application.id] = RuntimeForm(application.browser_page_id, form, registry, None, all_runtime_refs)
                self._transition(db, application, ApplicationState.NEEDS_USER.value, "required_answers_missing", {"fields": needs_user})
                event_hub.publish("applications", {"type": "questions_waiting", "application_id": application.id, "count": pending_count})
                return
            application.current_operation = "Verifying completed step"
            self._transition(db, application, ApplicationState.VERIFYING.value, "fields_verified")
            submit_action = next((action for action in form.actions if action.get("type") == "submit"), None)
            next_action = next((action for action in form.actions if action.get("type") == "next"), None)
            if next_action and not submit_action:
                result = await executor.click_action(next_action["id"], expected_change=True)
                if not result.success:
                    screenshot = await browser_manager.capture_diagnostic(page, "application-%d-next-step" % application.id)
                    application.error = result.error
                    self._transition(db, application, ApplicationState.NEEDS_USER.value, "next_step_failed", {"error": result.error, "screenshot": screenshot})
                    return
                self._transition(db, application, ApplicationState.FORM_DISCOVERY.value, "next_step")
                step_index += 1
                application.current_step = str(step_index)
                await browser_manager.bind_application_page(application.id, page)
                db.commit()
                continue
            if not await self._wait_for_challenge(
                db, application, page, ApplicationState.VERIFYING.value,
            ):
                return
            validation_form, _ = await scanner.scan(page)
            invalid_count = await page.evaluate('''() => Array.from(document.querySelectorAll('input,textarea,select,[aria-invalid=true]')).filter(e=>{
              const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&!e.disabled&&
                (e.getAttribute('aria-invalid')==='true'||e.validity&&!e.validity.valid);
            }).length''')
            if validation_form.validation_errors or invalid_count:
                application.error = 'Visible validation errors remain. Correct the marked fields and resume.'
                emit('form.validation_error', application_id=application.id, count=invalid_count)
                self._transition(db, application, ApplicationState.NEEDS_USER.value, 'field_validation_failed', {'invalid_count':invalid_count})
                return
            application.review_data = self._review(db, application)
            application.review_data = {**application.review_data, 'final_submit_available':bool(submit_action),
                                       'test_guard_active':settings.live_application_test or not settings.allow_final_submit}
            application.reviewed_at = datetime.utcnow()
            application.current_operation = "Waiting for explicit review and approval"
            self.runtime[application.id] = RuntimeForm(
                application.browser_page_id, form, registry,
                submit_action["id"] if submit_action else None, all_runtime_refs,
            )
            self._transition(db, application, ApplicationState.REVIEW_REQUIRED.value, "review_ready")
            return
        application.error = "Application exceeded the bounded form rescan limit; inspect the visible page and resume"
        self._transition(db, application, ApplicationState.NEEDS_USER.value, "step_limit_reached")

    async def edit_answer(self, application_id: int, answer_id: int, value: Any) -> Application:
        async with self._lock(application_id):
            db = SessionLocal()
            try:
                application = db.get(Application, application_id)
                answer = db.get(ApplicationAnswer, answer_id)
                runtime = self.runtime.get(application_id)
                if application is None or answer is None or answer.application_id != application_id:
                    raise KeyError("Application answer not found")
                if application.status != ApplicationState.REVIEW_REQUIRED.value:
                    raise ValueError("Answers can only be edited during review")
                if runtime is None or answer.field_id not in runtime.field_refs:
                    raise RuntimeError("The live form field is no longer available; open the form and rescan")
                page = await browser_manager.resolve_active_application_page(application.id, runtime.page_id, application.target_url)
                field = runtime.field_refs[answer.field_id]
                if field.metadata.get('compound_id'):
                    raise ValueError('Committed language entries must be changed in the visible collection and rescanned')
                document = await self._document_for(db, application)
                if field.control_kind == 'FILE':
                    if not _is_cv_upload(field) or not document:
                        raise ValueError('This file field has no selected document binding')
                    value = document['path']
                executor = BrowserActionExecutor(page, runtime.registry, application.id, document)
                action = self._action_for(field, ProposedAnswer(field.id, field.question, value, "MANUAL", 1.0))
                result = await executor.execute(action, field, value)
                if not result.success:
                    raise RuntimeError(result.error or "Browser field update failed")
                answer.value = value
                answer.source = "USER_CONFIRMED"
                answer.manual_override = True
                answer.verified = True
                answer.confidence = 1.0
                answer.status = "VERIFIED"
                answer.reason = "Confirmed by the user during review."
                concept = question_concept(answer.question)
                memory = db.execute(select(QuestionMemory).where(
                    QuestionMemory.candidate_id == application.candidate_id,
                    QuestionMemory.concept == concept,
                )).scalar_one_or_none()
                if memory is None:
                    memory = QuestionMemory(
                        candidate_id=application.candidate_id,
                        concept=concept,
                        question_fingerprint=stable_hash(answer.question.casefold()),
                        value=value,
                        source="USER_CONFIRMED",
                        confidence=1.0,
                        confirmed_at=datetime.utcnow(),
                    )
                    db.add(memory)
                else:
                    memory.value = value
                    memory.source = "USER_CONFIRMED"
                    memory.confidence = 1.0
                    memory.confirmed_at = datetime.utcnow()
                application.review_data = self._review(db, application)
                db.commit()
                self._publish(application)
                return application
            finally:
                db.close()

    async def submit(self, application_id: int) -> Application:
        from app.browser.action_safety import require_submission_allowed
        require_submission_allowed(application_id)
        async with self._lock(application_id):
            db = SessionLocal()
            try:
                application = db.get(Application, application_id)
                if application is None:
                    raise KeyError("Application not found")
                if application.status == ApplicationState.SUBMITTED.value:
                    return application
                if (application.review_data or {}).get('live_application_test') or db.execute(select(ApplicationAnswer.id).where(
                        ApplicationAnswer.application_id == application.id,
                        ApplicationAnswer.source.like('SYNTHETIC_TEST%'))).first():
                    from app.browser.action_safety import FinalSubmitBlocked
                    raise FinalSubmitBlocked()
                if application.status != ApplicationState.REVIEW_REQUIRED.value:
                    raise ValueError("Application is not awaiting review")
                runtime = self.runtime.get(application_id)
                if runtime is None or not runtime.submit_action_id:
                    raise RuntimeError("A verified final submit action is not available")
                page = await browser_manager.resolve_active_application_page(application.id, runtime.page_id, application.target_url)
                if page.url != application.target_url:
                    raise RuntimeError("Application page changed after review; rescan before submission")
                unverified = db.execute(select(ApplicationAnswer).where(
                    ApplicationAnswer.application_id == application.id,
                    ApplicationAnswer.verified.is_(False),
                )).scalars().first()
                if unverified is not None:
                    raise RuntimeError("At least one answer is not verified in the real form")
                application.approved_at = datetime.utcnow()
                self._transition(db, application, ApplicationState.APPROVED.value, "user_approved")
                self._transition(db, application, ApplicationState.SUBMITTING.value, "final_submit_started")
                executor = BrowserActionExecutor(page, runtime.registry, application.id)
                before_url = page.url
                result = await executor.click_action(runtime.submit_action_id, expected_change=True, approved_final=True)
                body = (await page.locator("body").inner_text()).casefold()
                success_text = any(marker in body for marker in (
                    "application submitted", "application received", "thank you for applying",
                    "aplikacja została wysłana", "dziękujemy za aplikację", "zgłoszenie zostało wysłane",
                ))
                if not await self._wait_for_challenge(
                    db, application, page, ApplicationState.SUBMITTING.value,
                ):
                    return application
                if not result.success or not success_text:
                    screenshot = await browser_manager.capture_diagnostic(page, "application-%d-submit-unverified" % application.id)
                    application.error = "Final click occurred, but submission success could not be verified"
                    self._transition(db, application, ApplicationState.NEEDS_USER.value, "submission_unverified", {"screenshot": screenshot})
                    return application
                application.submitted_at = datetime.utcnow()
                application.target_url = page.url
                application.job.application_status = "SUBMITTED"
                application.error = None
                self._transition(db, application, ApplicationState.SUBMITTED.value, "submission_verified", {"url": page.url})
                await self.release(application.id)
                return application
            finally:
                db.close()


orchestrator = ApplicationOrchestrator()

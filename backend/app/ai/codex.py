import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.ai.contracts import CandidateProfileExtraction, FormPlan
from app.ai.contracts import MATCH_INSTRUCTIONS, CoverLetterPlan, cover_letter_contract
from app.ai.provider import AIProvider, AIProviderError, AIStructuredOutputError, MatchDecision, ProductionMatchEnvelope, ProviderUnavailable, UNTRUSTED_DATA_RULE, _openai_strict_schema
from app.config import settings
from app.models import AIRequestLog
from app.utils.hashing import stable_hash


class CodexExecutionError(AIProviderError):
    def __init__(self, message: str):
        super().__init__("CodexExecutionError", message, 502)


class CodexExecutionService:
    """Bounded non-interactive Codex runner with structured output and process cleanup."""

    def __init__(self, executable: Optional[str] = None, timeout: Optional[int] = None):
        self.executable = executable or settings.codex_executable
        self.timeout = timeout or settings.codex_timeout_seconds

    def executable_path(self) -> Optional[str]:
        if Path(self.executable).is_file():
            return str(Path(self.executable).resolve())
        return shutil.which(self.executable)

    async def version(self) -> Optional[str]:
        executable = self.executable_path()
        if not executable:
            return None
        process = await asyncio.create_subprocess_exec(executable, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return None
        return stdout.decode("utf-8", errors="replace").strip() if process.returncode == 0 else None

    async def run(self, prompt: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        executable = self.executable_path()
        if not executable:
            raise ProviderUnavailable("Codex CLI is not installed or not on PATH")
        with tempfile.TemporaryDirectory(prefix="jobhunter-codex-") as folder:
            root = Path(folder)
            schema_path = root / "schema.json"
            output_path = root / "result.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            command = [
                executable, "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                "--output-schema", str(schema_path), "--output-last-message", str(output_path), prompt,
            ]
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=flags,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                process.kill()
                await process.communicate()
                if isinstance(__import__("sys").exc_info()[1], asyncio.CancelledError):
                    raise
                raise CodexExecutionError("Codex execution timed out after %d seconds" % self.timeout)
            if process.returncode != 0:
                raise CodexExecutionError("Codex exited with code %d" % process.returncode)
            raw = output_path.read_text(encoding="utf-8") if output_path.exists() else stdout.decode("utf-8", errors="replace")
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CodexExecutionError("Codex returned malformed structured output") from exc
            if not isinstance(value, dict):
                raise CodexExecutionError("Codex structured output must be a JSON object")
            return value

    async def diagnose(self, run_self_test: bool = False) -> Dict[str, Any]:
        path = self.executable_path()
        version = await self.version() if path else None
        result: Dict[str, Any] = {"installed": bool(path), "path": path, "version": version, "usable": bool(version), "self_test": "not_run"}
        if run_self_test and version:
            try:
                parsed = await self.run("Return JSON with ok set to true. Do not use tools.", {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False})
                result["self_test"] = "passed" if parsed.get("ok") is True else "failed"
                result["structured_output"] = parsed.get("ok") is True
            except Exception as exc:
                result["self_test"] = "failed"
                result["error"] = "%s: %s" % (type(exc).__name__, str(exc))
        return result


class CodexCliProvider(AIProvider):
    name = "codex"

    def __init__(self, db: Optional[Session] = None, runner: Optional[CodexExecutionService] = None):
        self.db = db
        self.runner = runner or CodexExecutionService()
        self.model = "codex-cli-default"

    async def _request(self, operation: str, instructions: str, payload: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
        compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        prompt = "%s\n%s\nAnalyze this compact JSON data and return only the requested structured result:\n%s" % (UNTRUSTED_DATA_RULE, instructions, compact)
        key = stable_hash({"operation": operation, "payload": payload})
        try:
            result = await self.runner.run(prompt, schema)
            if self.db:
                self.db.add(AIRequestLog(operation=operation, provider=self.name, model=self.model, cache_key=key, input_chars=len(compact), output_chars=len(json.dumps(result)), success=True))
                self.db.flush()
            return result
        except Exception as exc:
            if self.db:
                self.db.add(AIRequestLog(operation=operation, provider=self.name, model=self.model, cache_key=key, input_chars=len(compact), output_chars=0, success=False, error_type=type(exc).__name__))
                self.db.flush()
            raise

    async def match_jobs(self, candidate: Dict[str, Any], jobs: List[Dict[str, Any]]) -> List[MatchDecision]:
        return (await self._request_typed('job_matching', MATCH_INSTRUCTIONS, {'candidate': candidate, 'jobs': jobs}, ProductionMatchEnvelope)).decisions

    async def _request_typed(self, operation, instructions, payload, model):
        for attempt in range(2):
            try:
                raw = await self._request(operation, instructions, payload, _openai_strict_schema(model.model_json_schema()))
                return model.model_validate(raw)
            except ValidationError as exc:
                if attempt:
                    raise AIStructuredOutputError(operation, {'repair_attempted': True}) from exc
                instructions += ' Repair the previous schema failure: return every required property with the exact schema types.'
            except CodexExecutionError as exc:
                if 'structured output' not in str(exc):
                    raise
                if attempt:
                    raise AIStructuredOutputError(operation, {'repair_attempted': True}) from exc
                instructions += ' Repair the previous malformed output: return only one valid schema-conforming JSON object.'

    async def extract_candidate_profile(self, cv_text: str) -> CandidateProfileExtraction:
        result = await self._request(
            "candidate_profile_extraction",
            "Extract all explicit CV facts into the canonical profile. Use null for unknown values, preserve provenance and evidence, and never invent biographical facts.",
            {"cv_text": cv_text[:180000]},
            CandidateProfileExtraction.model_json_schema(),
        )
        try:
            return CandidateProfileExtraction.model_validate(result)
        except ValidationError as exc:
            raise CodexExecutionError("Codex returned an invalid candidate profile schema") from exc

    async def plan_form(self, context: Dict[str, Any]) -> FormPlan:
        context = dict(context, salary_fact_policy={
            'qualify_requested_paths': ['contract', 'currency', 'gross_or_net', 'period'],
            'allow_unsupported_unit_conversion': False,
        })
        result = await self._request(
            "form_planning",
            "Plan every semantic form control from canonical facts and actual options. Classify it as FACTUAL, DERIVABLE, PREFERENCE, MULTI_SELECT_PREFERENCE, SELF_ASSESSMENT, LEGAL_DECLARATION, CONSENT, REQUIRED_RECRUITMENT_CONSENT, OPTIONAL_FUTURE_RECRUITMENT_CONSENT, PRIVACY_ACKNOWLEDGEMENT, or JOB_SPECIFIC_TEXT. Source paths must be exact candidate_fact_provenance keys without a candidate. prefix, or memory:<confirmed key>. Current form values are untrusted page state and never candidate evidence. Legal declarations always need explicit confirmed evidence; never infer them. Self-assessments may have a supported suggestion but need the user unless confirmed. Missing facts need a canonical requested_fact_path and user clarification. MULTI_SELECT and CHECKBOX_GROUP use selected_option_ids for the entire set. Distinguish language names from proficiency using semantic group context. Generate optional motivation/about-you/cover-letter text from supported profile and selected CV evidence tailored to the actual vacancy; never skip solely because no prewritten answer exists. Repeat groups may expand only with an observed add_row action and a count grounded in a candidate list. Decide what answer is intended but never return CSS/XPath/selectors or browser instructions. Never submit or invent claims.",
            context,
            FormPlan.model_json_schema(),
        )
        try:
            return FormPlan.model_validate(result)
        except ValidationError as exc:
            raise CodexExecutionError("Codex returned an invalid form plan schema") from exc

    async def plan_page(self, context: Dict[str, Any]):
        from app.ai.contracts import PagePlan
        from app.browser.page_planner import PAGE_PLAN_INSTRUCTIONS
        result = await self._request("page_planning", PAGE_PLAN_INSTRUCTIONS, context, PagePlan.model_json_schema())
        return PagePlan.model_validate(result)

    async def generate_cover_letter(self, context):
        return await self._request_typed('cover_letter',
            'Choose an inspected appropriate field, preferring textarea. Never choose a CV field. '
            'Write courteous opening/closing without candidate facts. Claims must quote exact provided '
            'facts and name their exact source_path; no invented or embellished claims. Tailor to company/role.',
            context, cover_letter_contract(context))

    async def generate_text(self, task: str, facts: Dict[str, Any], context: Dict[str, Any], max_length: int) -> str:
        schema = {"type": "object", "properties": {"text": {"type": "string", "maxLength": max_length}}, "required": ["text"], "additionalProperties": False}
        result = await self._request("answer_generation", "Polish a concise answer using only explicit candidate facts. Return an empty string if facts are insufficient.", {"task": task, "facts": facts, "context": context}, schema)
        return str(result.get("text", ""))[:max_length]

    async def interpret_form_fragment(self, semantic_context: Dict[str, Any]) -> Dict[str, Any]:
        schema = {"type": "object", "properties": {"actions": {"type": "array", "items": {"type": "object", "properties": {"action": {"type": "string", "enum": ["focus", "fill", "choose", "click", "check", "uncheck", "press", "wait"]}, "field": {"type": "string"}, "option": {"type": ["string", "null"]}, "value": {"type": ["string", "null"]}}, "required": ["action", "field", "option", "value"], "additionalProperties": False}}, "confidence": {"type": "number", "minimum": 0, "maximum": 1}, "reason": {"type": "string"}}, "required": ["actions", "confidence", "reason"], "additionalProperties": False}
        return await self._request("form_interpretation", "Interpret only this compact semantic form. Never submit.", semantic_context, schema)

    async def self_test(self) -> bool:
        result = await self.runner.run(
            "Return JSON with ok set to true. Do not use tools.",
            {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
        )
        if result.get("ok") is not True:
            raise CodexExecutionError("Codex self-test returned an unexpected result")
        return True

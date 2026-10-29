"""Application-owned UTF-8 letters with exact persisted evidence and file binding."""
from pathlib import Path
import hashlib

from app.ai.contracts import CoverLetterPlan
from app.candidate.fact_resolver import CandidateFactResolver
from app.config import REPOSITORY_ROOT, settings
from app.forms.answer_engine import ProposedAnswer
from app.utils.hashing import normalized_text, stable_hash

ROOT = REPOSITORY_ROOT / 'storage' / 'generated' / 'cover_letters'


def generated_path(application_id):
    return ROOT / ('%d_cover_letter.txt' % int(application_id))


def is_cv_field(field):
    name = normalized_text(field.question + ' ' + str(field.metadata.get('name', '')) + ' ' + str(field.metadata.get('html_id') or ''))
    if any(word in name for word in ('cover letter', 'motivation letter', 'list motywacyjny')):
        return False
    if field.type == 'file':
        name += ' ' + normalized_text(str(field.metadata.get('surrounding_text') or ''))
    return any(word in name for word in ('cv', 'resume', 'résumé', 'życiorys', 'zyciorys', 'curriculum vitae'))


def letter_field_mode(field):
    if is_cv_field(field):
        return None
    name = normalized_text(field.question + ' ' + str(field.metadata.get('name', '')))
    dedicated = any(word in name for word in ('cover letter', 'cover_letter', 'motivation letter', 'list motywacyjny', 'lettre de motivation'))
    generic = not field.required and any(word in name for word in ('additional attachment', 'additional document', 'supporting document', 'dodatkowe dokument'))
    if dedicated and (field.type == 'textarea' or field.control_kind in {'CONTENTEDITABLE', 'RICH_TEXT'}):
        return 'textarea'
    if field.type == 'file' and (dedicated or generic):
        accepts = {item.strip().casefold() for item in str(field.metadata.get('accept') or '').split(',') if item.strip()}
        if not accepts or accepts & {'.txt', 'text/plain', 'text/*', '*/*', '*'}:
            return 'txt_attachment'
    return None


def is_letter_field(field):
    name = normalized_text(field.question + ' ' + str(field.metadata.get('name', '')))
    return not is_cv_field(field) and any(word in name for word in ('cover letter', 'cover_letter', 'motivation letter', 'list motywacyjny', 'lettre de motivation'))


class CoverLetterService:
    async def prepare(self, application, fields, provider):
        if settings.cover_letter_mode == 'off':
            return None
        eligible = [field for field in fields if letter_field_mode(field)]
        if not eligible:
            return None
        # A dedicated textarea always takes priority over upload targets.
        text_fields = [field for field in eligible if letter_field_mode(field) == 'textarea']
        if text_fields:
            eligible = text_fields
        candidate = application.candidate
        resolver = CandidateFactResolver(candidate.data, candidate.facts)
        facts = {}
        for fact in candidate.facts:
            if not fact.path.startswith(('employment.', 'projects.', 'skills.', 'skill_groups.', 'education.', 'languages.', 'professional_summary')):
                continue
            resolved = resolver.resolve(fact.path)
            if resolved.exists and resolved.source in {'CV_EXPLICIT', 'USER_ENTERED', 'USER_CONFIRMED'} and isinstance(resolved.value, str) and resolved.value.strip():
                if len(resolved.value) <= 500:
                    facts[resolved.canonical_path] = resolved.value
        if not facts:
            return None
        key = stable_hash({'job': application.job.content_hash, 'cv': application.selected_cv_document_id,
                           'facts': facts, 'fields': [(field.id, letter_field_mode(field)) for field in eligible]})
        stored = (application.review_data or {}).get('cover_letter') or {}
        if stored.get('key') == key:
            selected = next((field for field in eligible if field.id == stored.get('field_id')), None)
            if selected:
                return self._binding(application, selected, stored)
        if not hasattr(provider, 'generate_cover_letter'):
            return None
        application.current_operation = 'Generating cover letter'
        raw = await provider.generate_cover_letter({'mode': settings.cover_letter_mode,
            'job': {'company': application.job.company, 'title': application.job.title, 'requirements': application.job.requirements},
            'facts': facts, 'recommended': (application.review_data or {}).get('evaluation', {}).get('recommended_cover_letter', False),
            'fields': [{'id': field.id, 'mode': letter_field_mode(field), 'required': field.required, 'question': field.question} for field in eligible]})
        plan = CoverLetterPlan.model_validate(raw)
        if plan.mode == 'none':
            return None
        field = next((field for field in eligible if field.id == plan.field_id), None)
        if field is None or letter_field_mode(field) != plan.mode:
            raise ValueError('Cover-letter AI selected an inappropriate attachment field')
        claims = []
        paths = []
        for claim in plan.claims[:4]:
            exact = facts.get(claim.source_path)
            if exact is None:
                raise ValueError('Cover-letter claim is not an exact persisted fact')
            # Model prose never becomes a claim. Render only the exact persisted
            # value named by its validated path, just as opening prose is ignored.
            claims.append(exact); paths.append(claim.source_path)
        if not claims:
            return None
        # AI selects job-relevant evidence; trusted phrasing makes it impossible
        # for unrestricted opening/closing prose to introduce new candidate facts.
        text = ('Dear recruitment team at %s,\n\nI would like to apply for %s. '
                'Relevant details from my CV/profile:\n\n%s\n\n'
                'I would welcome the opportunity to discuss this role.\n') % (
                    application.job.company, application.job.title, '\n'.join('- ' + claim for claim in claims))
        ROOT.mkdir(parents=True, exist_ok=True)
        path = generated_path(application.id)
        path.write_text(text, encoding='utf-8')
        stored = {'key': key, 'mode': plan.mode, 'field_id': field.id, 'text': text,
                  'path': str(path), 'source_paths': paths, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        application.review_data = {**(application.review_data or {}), 'cover_letter': stored}
        return self._binding(application, field, stored)

    def _binding(self, application, field, stored):
        path = generated_path(application.id)
        if not path.is_file():
            ROOT.mkdir(parents=True, exist_ok=True)
            path.write_text(stored['text'], encoding='utf-8')
        answer = ProposedAnswer(field.id, field.question, str(path) if stored['mode'] == 'txt_attachment' else stored['text'],
                                'GENERATED_COVER_LETTER', 1.0, source_paths=stored['source_paths'],
                                action='upload' if stored['mode'] == 'txt_attachment' else 'fill',
                                execution_confidence='HIGH_CONFIDENCE', question_category='JOB_SPECIFIC_TEXT')
        return {'field': field, 'answer': answer, 'text': stored['text'], 'path': str(path),
                'document': {'path': str(path), 'kind': 'COVER_LETTER', 'sha256': stored['sha256'], 'name': path.name}}

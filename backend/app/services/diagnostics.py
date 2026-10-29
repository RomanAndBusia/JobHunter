"""Bounded, redacted runtime diagnostics; never a store of candidate/form values."""
from collections import deque
from contextvars import ContextVar
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import re
import traceback
import uuid

from app.config import REPOSITORY_ROOT, settings

scope = ContextVar('jobhunter_diagnostic_scope', default={})
RUN_ID = uuid.uuid4().hex
records = deque(maxlen=2000)
PRIVATE_KEYS = re.compile(r'api.?key|authorization|cookie|password|oauth|access.?token|refresh.?token|secret|cv_text|candidate_data|form_value|^value$|^answer$', re.I)


def redact(value):
    if isinstance(value, dict):
        return {str(k): v if k in {'cookie_count', 'justjoin_cookie_count'} and isinstance(v, int)
                else '[REDACTED]' if PRIVATE_KEYS.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value[:100]]
    if not isinstance(value, str):
        return value
    text = value[:24000]
    from urllib.parse import urlsplit, urlunsplit
    if text.startswith(('http://', 'https://')):
        parsed = urlsplit(text)
        if parsed.hostname in {'sso.rocketjobs.pl', 'profile.justjoin.it'} or re.search(r'(^|&)(code|token|state|redirectTo|code_challenge)=', parsed.query, re.I):
            text = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
    if text.lstrip().startswith(('{', '[')):
        try:
            return json.dumps(redact(json.loads(text)), ensure_ascii=False)
        except (ValueError, TypeError):
            pass
    if settings.ai_api_key:
        text = text.replace(settings.ai_api_key, '[REDACTED]')
    text = re.sub(r'(?i)(bearer\s+|(?:api_key|password|access_token|refresh_token|token|secret|session)\s*[=:]\s*)[^\s&\"\']+', r'\1[REDACTED]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[REDACTED]', text)
    text = re.sub(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[EMAIL]', text)
    text = re.sub(r'(?<![\w])\+?\d{9,15}(?![\w])', '[PHONE]', text)
    text = re.sub(r'(?i)(fill\(|value=|password=)[^\n]+', r'\1[REDACTED]', text)
    return text


def emit(message, level='INFO', **context):
    record = redact({'timestamp': datetime.utcnow().isoformat() + 'Z', 'level': level,
                     'run_id': RUN_ID, **scope.get(), 'message': message, **context})
    records.append(record)
    logging.getLogger('app.runtime').log(getattr(logging, level), message, extra={'context': context, 'diagnostic_record': record})
    return record


class DiagnosticHandler(logging.Handler):
    def emit(self, record):
        if hasattr(record, 'diagnostic_record'):
            return
        context = getattr(record, 'context', {})
        item = {'timestamp': datetime.utcfromtimestamp(record.created).isoformat() + 'Z', 'run_id': RUN_ID,
                **scope.get(), **context, 'level': record.levelname, 'subsystem': record.name, 'message': record.getMessage()}
        if record.exc_info:
            item.update(exception_type=record.exc_info[0].__name__, stack_trace=''.join(traceback.format_exception(*record.exc_info)))
        records.append(redact(item))


def install(root):
    root.addHandler(DiagnosticHandler())
    folder = REPOSITORY_ROOT / 'storage' / 'logs'
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(str(folder / 'runtime.jsonl'), maxBytes=2*1024*1024, backupCount=2, encoding='utf-8')
    from app.logging import JsonFormatter
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


def events(application_id=None, vacancy_id=None, full_run=False):
    return [item for item in records if full_run or
            (application_id is not None and item.get('application_id') == application_id) or
            (vacancy_id is not None and item.get('vacancy_id', item.get('job_id')) == vacancy_id)][-100:]

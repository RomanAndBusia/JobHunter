"""Trusted, hash-bound format adaptation of the application-pinned CV."""
import asyncio
from collections import Counter
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import unicodedata

from pypdf import PdfReader
from app.config import REPOSITORY_ROOT
from app.errors import JobHunterError

_locks = {}
ROOT = REPOSITORY_ROOT / 'storage' / 'derived-cvs'


def accepts(accept, suffix):
    options = {part.strip().casefold() for part in accept.split(',') if part.strip()}
    mime = {'.pdf':'application/pdf', '.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}.get(suffix)
    return not options or bool(options & {'*', '*/*', suffix, mime})


def _words(text):
    return Counter(re.findall(r'\w+', unicodedata.normalize('NFKC', text).casefold()))


def verify_pdf(path, source_text):
    reader = PdfReader(str(path))
    text = '\n'.join(page.extract_text() or '' for page in reader.pages)
    expected, actual = _words(source_text), _words(text)
    if not expected or not reader.pages or not text.strip() or sum((expected-actual).values()) > max(3, sum(expected.values())*.02):
        raise JobHunterError('CV_CONVERSION_UNVERIFIED', 'The converted CV could not be verified against its source text. Select a PDF CV.', 409, 'document.convert')


def _word_export(source, destination):
    if sys.platform != 'win32':
        raise JobHunterError('CV_CONVERSION_UNAVAILABLE', 'This field requires PDF. Select a PDF CV or use installed Microsoft Word on Windows for conversion.', 409, 'document.convert')
    try:
        result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-File',
                                 str(Path(__file__).with_name('export_cv_pdf.ps1')),
                                 '-Source', str(source), '-Destination', str(destination)],
                                capture_output=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode or not destination.is_file(): raise RuntimeError('Word export failed')
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        raise JobHunterError('CV_CONVERSION_UNAVAILABLE', 'Microsoft Word could not export the pinned CV. Select a PDF CV and resume.', 409, 'document.convert') from exc


async def resolve_upload_document(document, field):
    """The model never chooses a path, a document, or a conversion input."""
    source = Path(document['path']).resolve()
    expected_hash = document.get('source_sha256') or document['sha256']
    if hashlib.sha256(source.read_bytes()).hexdigest() != document['sha256']:
        raise JobHunterError('CV_SOURCE_CHANGED', 'The pinned CV file changed on disk. Select the correct document again.', 409, 'document.resolve')
    accept = str(field.metadata.get('accept') or '')
    if accepts(accept, source.suffix.casefold()): return dict(document)
    if source.suffix.casefold() != '.docx' or not accepts(accept, '.pdf'):
        raise JobHunterError('CV_FORMAT_UNSUPPORTED', 'The website does not accept the selected CV format. Select an accepted CV document.', 409, 'document.resolve')
    key = '%d-%s' % (document['id'], expected_hash)
    async with _locks.setdefault(key, asyncio.Lock()):
        folder = ROOT / key
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / (Path(document['name']).stem + '.pdf')
        if not destination.exists():
            temporary = destination.with_suffix('.partial.pdf')
            try:
                await asyncio.to_thread(_word_export, source, temporary)
                verify_pdf(temporary, document.get('extracted_text', ''))
                temporary.replace(destination)
            finally:
                if temporary.exists(): temporary.unlink()
        verify_pdf(destination, document.get('extracted_text', ''))
        return {**document, 'path':str(destination), 'name':destination.name,
                'source_name':document['name'], 'source_sha256':expected_hash,
                'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(), 'conversion':'DOCX_TO_PDF'}

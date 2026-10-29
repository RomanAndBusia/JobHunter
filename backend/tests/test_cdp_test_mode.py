"""Verify the isolated launcher waits passively and keeps its connected session."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.browser.challenge import ChallengeState


@pytest.mark.asyncio
@pytest.mark.parametrize("challenge", [False, True])
async def test_cdp_probe_retains_context_and_waits_without_challenge_actions(monkeypatch, tmp_path, challenge):
    source = Path(__file__).parents[2] / "test" / "cdp_app.py"
    spec = importlib.util.spec_from_file_location("cdp_test_app", source)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.RESULT = tmp_path / "result.json"
    module.CHROME_PROCESS = SimpleNamespace(pid=123)
    navigations = []
    connects = []
    detections = []
    context = SimpleNamespace()

    class Page:
        url = "https://www.pracuj.pl/"
        async def goto(self, url, **_kwargs):
            navigations.append(url)
            return SimpleNamespace(status=200)
        async def wait_for_timeout(self, _ms):
            pass
        async def title(self):
            return "Pracuj.pl"
        def is_closed(self):
            return False

    page = Page(); page.context = context
    async def new_page():
        return page
    context.new_page = new_page
    browser = SimpleNamespace(contexts=[context])
    async def connect(endpoint):
        connects.append(endpoint)
        return browser
    playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
    signals = [challenge, challenge, False] if challenge else [False]
    async def detect(_self, inspected, *args, **kwargs):
        detections.append(inspected)
        return ChallengeState(signals.pop(0))
    async def sleep(_seconds):
        pass
    monkeypatch.setattr("app.browser.challenge.ChallengeDetector.detect", detect)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    previous_level = __import__('logging').getLogger('app.browser.challenge').level
    try:
        returned_context, status = await module.connect_and_probe(playwright, None)
    finally:
        __import__('logging').getLogger('app.browser.challenge').setLevel(previous_level)
    assert returned_context is context
    assert not status.configured
    assert connects == ["http://127.0.0.1:9333"]
    assert navigations == ["https://www.pracuj.pl/"]
    assert all(inspected is page for inspected in detections)
    result = __import__('json').loads(module.RESULT.read_text())
    assert result['same_context'] and result['navigation_success']
    assert result['initial_challenge'] is challenge


def test_slot_migration_preserves_history_and_owned_rows(tmp_path):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text
    source = Path(__file__).parents[1] / 'alembic' / 'versions' / '0007_direct_slot.py'
    spec = importlib.util.spec_from_file_location('slot_migration', source)
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    engine = create_engine('sqlite:///' + str(tmp_path / 'legacy.db'))
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE jobs (id INTEGER PRIMARY KEY, source TEXT)'))
        connection.execute(text('CREATE TABLE applications (id INTEGER PRIMARY KEY, candidate_id INTEGER, job_id INTEGER, status TEXT)'))
        connection.execute(text('CREATE TABLE application_answers (id INTEGER PRIMARY KEY, application_id INTEGER REFERENCES applications(id) ON DELETE CASCADE)'))
        connection.execute(text("INSERT INTO jobs VALUES (1, 'direct'), (2, 'direct'), (3, 'pracuj')"))
        connection.execute(text("INSERT INTO applications VALUES (1, 1, 1, 'FAILED'), (2, 1, 2, 'ABORTED'), (3, 2, 3, 'READY')"))
        connection.execute(text('INSERT INTO application_answers VALUES (1, 2)'))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert connection.execute(text('SELECT id FROM applications WHERE direct_slot_active=1')).all() == [(2,)]
        assert connection.execute(text('SELECT status FROM applications ORDER BY id')).all() == [('FAILED',), ('ABORTED',), ('READY',)]
        assert connection.execute(text('SELECT * FROM application_answers')).all() == [(1, 2)]
    engine.dispose()

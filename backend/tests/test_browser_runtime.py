from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.browser.chrome import ChromeProcessManager, find_chrome, BrowserConnectionError
from app.browser.runtime import connect_chrome
from app.browser.manager import BrowserManager, BrowserSessionLostError
from app.config import load_settings, settings, SettingsError


def test_cdp_is_default_headed_and_stealth_retired(tmp_path):
    configured = load_settings(tmp_path / '.env', environ={})
    assert configured.browser_mode == 'cdp'
    assert configured.browser_stealth is False
    assert configured.browser_profile.is_absolute()


@pytest.mark.parametrize('values', [{'JOBHUNTER_BROWSER_MODE': 'chromium'}, {'JOBHUNTER_CDP_HOST': '0.0.0.0'}, {'JOBHUNTER_COVER_LETTER_MODE': 'sometimes'}])
def test_browser_configuration_rejects_unsafe_modes(tmp_path, values):
    with pytest.raises(SettingsError):
        load_settings(tmp_path / '.env', environ=values)


def test_chrome_discovery_uses_installed_windows_path(tmp_path, monkeypatch):
    chrome = tmp_path / 'Google' / 'Chrome' / 'Application' / 'chrome.exe'
    chrome.parent.mkdir(parents=True); chrome.write_bytes(b'chrome')
    monkeypatch.setenv('PROGRAMFILES', str(tmp_path))
    assert find_chrome() == chrome.resolve()
    with pytest.raises(BrowserConnectionError):
        find_chrome(str(tmp_path / 'missing.exe'))


@pytest.mark.asyncio
async def test_chrome_allocates_port_and_preserves_profile(tmp_path, monkeypatch):
    profile = tmp_path / 'chrome-profile'; profile.mkdir()
    marker = profile / 'session-marker'; marker.write_text('keep')
    chrome = tmp_path / 'chrome.exe'; chrome.write_text('fake')
    manager = ChromeProcessManager(replace(settings, browser_profile=profile, chrome_path=str(chrome)))
    captured = []
    class Process:
        pid = 123
        def poll(self): return None
    def spawn(args):
        captured.extend(args)
        (profile / 'DevToolsActivePort').write_text('45678\n/devtools/browser/test')
        return Process()
    async def healthy(): return bool(manager.endpoint)
    monkeypatch.setattr('app.browser.chrome.subprocess.Popen', spawn)
    monkeypatch.setattr(manager, 'healthy', healthy)
    endpoint = await manager.ensure_started()
    assert endpoint == 'http://127.0.0.1:45678'
    assert '--remote-debugging-port=0' in captured
    assert '--remote-debugging-address=127.0.0.1' in captured
    assert not any('--headless' in arg for arg in captured)
    assert marker.read_text() == 'keep'
    assert await manager.ensure_started() == endpoint
    assert captured.count(str(chrome)) == 1


@pytest.mark.asyncio
async def test_cdp_connect_uses_default_persistent_context():
    context = object(); endpoints = []
    browser = SimpleNamespace(contexts=[context])
    async def connect(endpoint, **kwargs): endpoints.append(endpoint); return browser
    async def start(): return 'http://127.0.0.1:41234'
    attached, returned = await connect_chrome(SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect)), SimpleNamespace(ensure_started=start))
    assert attached is browser and returned is context
    assert endpoints == ['http://127.0.0.1:41234']


@pytest.mark.asyncio
async def test_manager_reconnects_without_restarting_chrome(monkeypatch):
    manager = BrowserManager()
    page = SimpleNamespace(url='https://example.test/form', is_closed=lambda: False, on=lambda *args: None)
    context = SimpleNamespace(pages=[page], on=lambda *args: None)
    manager.context = context; manager.pages['owned-page'] = page
    manager.browser = SimpleNamespace(is_connected=lambda: False)
    manager._playwright = object()
    calls = []
    async def connect(playwright, chrome):
        calls.append(chrome)
        return SimpleNamespace(is_connected=lambda: True), context
    monkeypatch.setattr('app.browser.manager.connect_chrome', connect)
    await manager.ensure_started()
    assert manager.pages['owned-page'] is page
    assert calls == [manager.chrome]


def test_no_production_direct_launch_or_stealth():
    from pathlib import Path
    root = Path(__file__).parents[1] / 'app'
    for path in root.rglob('*.py'):
        source = path.read_text(encoding='utf-8')
        assert 'chromium.launch(' not in source
        assert 'launch_persistent_context(' not in source
        assert 'playwright_stealth' not in source


class ManagedFakePage:
    def __init__(self):
        self.closed = False
        self.close_calls = 0
        self.url = "https://example.test/application"

    def is_closed(self):
        return self.closed

    async def close(self):
        self.close_calls += 1
        self.closed = True


class ManagedFakeContext:
    def __init__(self, pages):
        self.pages = pages


@pytest.mark.asyncio
async def test_application_page_cleanup_is_idempotent():
    manager = BrowserManager()
    page = ManagedFakePage()
    manager.context = ManagedFakeContext([page])
    manager.pages["page-1"] = page
    assert await manager.close_page("page-1") is True
    assert await manager.close_page("page-1") is False
    assert page.close_calls == 1


def test_dead_page_raises_typed_browser_session_error():
    manager = BrowserManager()
    page = ManagedFakePage()
    page.closed = True
    manager.context = ManagedFakeContext([page])
    manager.pages["page-1"] = page
    with pytest.raises(BrowserSessionLostError) as raised:
        manager.get_page("page-1")
    assert raised.value.error_type == "browser_session_lost"
    assert "Target page" not in raised.value.safe_message


@pytest.mark.asyncio
async def test_target_identity_rebinds_duplicate_url_tabs_without_new_pages(monkeypatch):
    manager=BrowserManager()
    def page(target):
        return SimpleNamespace(target=target, url='https://example.test/form', is_closed=lambda:False, on=lambda *args:None)
    original=[page('A'),page('B')]
    async def session(tab):
        async def send(method): return {'targetInfo':{'targetId':tab.target}}
        async def detach(): pass
        return SimpleNamespace(send=send,detach=detach)
    context=SimpleNamespace(pages=original,on=lambda *args:None,new_cdp_session=session)
    manager.context=context; manager._playwright=object()
    manager.browser=SimpleNamespace(is_connected=lambda:False)
    for tab in original: await manager._bind_page(tab)
    replacement=[page('A'),page('B')]; context.pages=replacement
    async def connect(*args): return SimpleNamespace(is_connected=lambda:True,contexts=[context]),context
    monkeypatch.setattr('app.browser.manager.connect_chrome',connect)
    await manager.ensure_started(); await manager.ensure_started()
    assert set(manager.pages)=={'cdp-A','cdp-B'}
    assert manager.get_page('cdp-A') is replacement[0] and len(context.pages)==2


@pytest.mark.asyncio
async def test_stale_pid_does_not_authorize_attach_or_termination(tmp_path, monkeypatch):
    import json
    profile=tmp_path/'profile'; profile.mkdir()
    (profile/'jobhunter-cdp.json').write_text(json.dumps({'pid':123,'port':2222,'browser_id':'old'}))
    chrome=tmp_path/'chrome.exe'; chrome.write_text('fake')
    manager=ChromeProcessManager(replace(settings,browser_profile=profile,chrome_path=str(chrome)))
    monkeypatch.setattr(manager,'_owned',lambda pid:False)
    class Process:
        pid=456
        def poll(self): return None
    def spawn(args):
        (profile/'DevToolsActivePort').write_text('3333\n/devtools/browser/new')
        return Process()
    async def healthy(): return manager.endpoint=='http://127.0.0.1:3333'
    async def unexpected_shutdown(): pytest.fail('Never kill an unrelated reused PID')
    monkeypatch.setattr('app.browser.chrome.subprocess.Popen',spawn)
    monkeypatch.setattr(manager,'healthy',healthy)
    monkeypatch.setattr(manager,'shutdown',unexpected_shutdown)
    assert await manager.ensure_started()=='http://127.0.0.1:3333'
    assert manager.pid==456

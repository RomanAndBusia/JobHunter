"""Isolated Playwright subprocess diagnostic entrypoint for Windows-safe startup."""
import argparse
import asyncio
import json
import sys
from typing import Any, Dict


async def browser_self_test() -> Dict[str, Any]:
    from dataclasses import replace
    from pathlib import Path
    import uuid
    from app.browser.manager import BrowserManager
    from app.config import REPOSITORY_ROOT, settings
    probe = await asyncio.create_subprocess_exec(sys.executable, "-c", "print('ok')", stdout=asyncio.subprocess.PIPE)
    stdout, _ = await probe.communicate()
    manager = BrowserManager(replace(settings, browser_profile=REPOSITORY_ROOT / 'storage' / 'diagnostics' / ('cdp-' + uuid.uuid4().hex), frontend_url='about:blank'))
    try:
        await manager.ensure_started()
        page = await manager.new_application_page()
        await page.set_content('<title>JobHunter CDP self-test</title><main>ok</main>')
        success = await page.title() == 'JobHunter CDP self-test'
        return {'ok': success, 'worker_started': True, 'subprocess_support': probe.returncode == 0 and stdout.strip() == b'ok',
                'playwright_started': True, 'chrome_found': True, 'cdp_connected': True, 'page_created': success, 'browser': manager.chrome.payload()}
    finally:
        await manager.stop()
        await manager.chrome.shutdown()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        try:
            print(json.dumps(await browser_self_test()))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "worker_started": True, "error": "%s: %s" % (type(exc).__name__, str(exc))}))
            return 1
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))

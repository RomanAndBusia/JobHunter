"""Test-only Chrome launch injection. Production BrowserManager stays unchanged."""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit
import shutil
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
PORT = 9333
PROFILE = ROOT / "test" / "chrome-profile"
RESULT = ROOT / "test" / "cdp-result.json"


def chrome_executable():
    candidates = [Path(os.environ.get(key, "")) / suffix for key, suffix in (
        ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    )]
    located = shutil.which("chrome.exe")
    if located:
        candidates.append(Path(located))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("Google Chrome was not found. Install Chrome to run this test.")


def launch_chrome():
    with socket.socket() as check:
        if check.connect_ex(("127.0.0.1", PORT)) == 0:
            raise RuntimeError("CDP port 9333 is occupied. Close the previous test Chrome first; no existing process will be attached or killed.")
    chrome = chrome_executable()
    PROFILE.mkdir(parents=True, exist_ok=True)
    print("Chrome: %s\nCDP: http://127.0.0.1:%d\nProfile: %s" % (chrome, PORT, PROFILE), flush=True)
    process = subprocess.Popen([
        str(chrome), "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=%d" % PORT, "--user-data-dir=%s" % PROFILE,
        "--no-first-run", "--no-default-browser-check", "about:blank",
    ])
    for _ in range(100):
        with socket.socket() as check:
            if check.connect_ex(("127.0.0.1", PORT)) == 0:
                return process
        if process.poll() is not None:
            raise RuntimeError("The new test Chrome exited before CDP became available.")
        time.sleep(0.2)
    raise RuntimeError("Chrome did not expose CDP port 9333 within 20 seconds.")


async def connect_and_probe(playwright, _config):
    from app.browser.challenge import ChallengeDetector
    from app.browser.runtime import BrowserStealthStatus
    # Polling must not flood the concise test output with unchanged signals.
    logging.getLogger("app.browser.challenge").setLevel(logging.WARNING)
    try:
        browser = await playwright.chromium.connect_over_cdp("http://127.0.0.1:9333")
    except Exception as exc:
        print("CDP connection: failure (%s)" % type(exc).__name__, flush=True)
        RESULT.write_text(json.dumps({"cdp_connected": False, "error_type": type(exc).__name__}), encoding="utf-8")
        raise
    print("CDP connection: success", flush=True)
    if not browser.contexts:
        raise RuntimeError("Chrome CDP did not expose its persistent context.")
    context = browser.contexts[0]
    page = await context.new_page()
    result = {"cdp_connected": True, "chrome_pid": CHROME_PROCESS.pid, "port": PORT}
    try:
        response = await page.goto("https://www.pracuj.pl/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        detector = ChallengeDetector()
        initial = await detector.detect(page, response.status if response else None)
        result["initial_challenge"] = initial.detected
        result["manual_interaction_required"] = initial.detected
        result["status"] = "waiting_for_manual_verification" if initial.detected else "loaded"
        RESULT.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print("URL: %s\nVerification challenge: %s" % (page.url, initial.detected), flush=True)
        if initial.detected:
            print("MANUAL VERIFICATION REQUIRED: complete verification in this Chrome window. Automation is paused; the same session will resume automatically.", flush=True)
            while (await detector.detect(page)).detected:
                if page.is_closed():
                    raise RuntimeError("The verification page was closed before manual completion.")
                await asyncio.sleep(2)
            print("Manual verification cleared; same Chrome/CDP/context/page retained.", flush=True)
        result.update(url=page.url, title=await page.title(), challenge_cleared=True, status="complete",
                      navigation_success=urlsplit(page.url).hostname in {"pracuj.pl", "www.pracuj.pl"},
                      same_context=page.context is context, context_count=len(browser.contexts))
        print("After verification: %s\nNavigation success: %s" % (page.url, result["navigation_success"]), flush=True)
        RESULT.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return context, BrowserStealthStatus(configured=False)
    except Exception as exc:
        result.update(error_type=type(exc).__name__, message=str(exc))
        RESULT.write_text(json.dumps(result, indent=2), encoding="utf-8")
        raise


async def probe_only():
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        await connect_and_probe(playwright, None)
    print("Test complete. Chrome remains open for inspection.", flush=True)


def main():
    global CHROME_PROCESS
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe-only", action="store_true")
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    CHROME_PROCESS = launch_chrome()
    if args.probe_only:
        asyncio.run(probe_only())
        return
    os.environ["JOBHUNTER_BACKEND_URL"] = "http://127.0.0.1:8001"
    os.environ["JOBHUNTER_FRONTEND_URL"] = "http://127.0.0.1:5174"
    os.environ["JOBHUNTER_DATABASE_URL"] = "sqlite:///" + (ROOT / "test" / "jobhunter.db").as_posix()
    os.environ["JOBHUNTER_BROWSER_AUTOSTART"] = "true"
    fresh_database = not (ROOT / "test" / "jobhunter.db").exists()
    if fresh_database:
        # The historical initial migration imports live metadata. Bootstrap a new
        # isolated database directly; existing test databases use normal upgrades.
        from app.main import initialize_database
        initialize_database()
    subprocess.run([sys.executable, "-m", "alembic", "stamp" if fresh_database else "upgrade", "head"], cwd=str(ROOT / "backend"), check=True)
    # Patch only this test process, before app.main/router/orchestrator import.
    import app.browser.manager as manager
    async def test_connection(playwright, process_manager):
        context, _status = await connect_and_probe(playwright, None)
        process_manager.pid = CHROME_PROCESS.pid
        process_manager.endpoint = 'http://127.0.0.1:9333'
        return context.browser, context
    manager.connect_chrome = test_connection
    from app.main import app
    import uvicorn
    frontend = subprocess.Popen(["cmd.exe", "/c", "npm.cmd", "run", "dev", "--", "--config", "../test/vite.config.mjs", "--port", "5174", "--strictPort"], cwd=str(ROOT / "frontend"))
    print("Test JobHunter: http://127.0.0.1:5174/apply-url (API :8001). Production browser startup is unchanged. Ctrl+C stops the test app.", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=8001, reload=False, loop="asyncio", access_log=False)
    finally:
        # PID-scoped cleanup of the test frontend tree only; Chrome is kept open.
        subprocess.run(["taskkill", "/PID", str(frontend.pid), "/T", "/F"], capture_output=True)


if __name__ == "__main__":
    main()

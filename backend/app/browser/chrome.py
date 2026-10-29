"""Own installed headed Chrome, its persistent profile and loopback CDP endpoint."""
import asyncio
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Optional

import httpx

from app.config import Settings
from app.errors import JobHunterError


class BrowserConnectionError(JobHunterError):
    def __init__(self, message):
        super().__init__("browser_connection_error", message, 503, "browser.cdp")


def find_chrome(configured: str = "") -> Path:
    if configured:
        path = Path(configured)
        if path.is_file():
            return path.resolve()
        raise BrowserConnectionError("JOBHUNTER_CHROME_PATH does not point to an installed executable.")
    candidates = [Path(os.environ.get(key, "")) / "Google/Chrome/Application/chrome.exe"
                  for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    for command in ("chrome.exe", "google-chrome", "google-chrome-stable"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise BrowserConnectionError("Google Chrome was not found. Install Chrome or configure JOBHUNTER_CHROME_PATH.")


class ChromeProcessManager:
    def __init__(self, config: Settings):
        self.config = config
        self.profile = config.browser_profile.resolve()
        self.state_file = self.profile / "jobhunter-cdp.json"
        self.process: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.endpoint: Optional[str] = None
        self.browser_id: Optional[str] = None
        self.executable: Optional[str] = None
        self._lock = asyncio.Lock()

    def _owned(self, pid: int) -> bool:
        if self.process is not None and self.process.pid == pid:
            return self.process.poll() is None
        if sys.platform != "win32":
            try:
                args = Path("/proc/%d/cmdline" % pid).read_bytes().decode().replace("\0", " ")
            except OSError:
                return False
        else:
            # PID reuse is never sufficient authority to attach or terminate.
            result = subprocess.run([
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-CimInstance Win32_Process -Filter 'ProcessId = %d').CommandLine" % pid,
            ], capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            args = result.stdout
        profile_argument = r'--user-data-dir="?' + re.escape(str(self.profile)) + r'(?:"|\s|$)'
        return bool(re.search(profile_argument, args, re.I)) and "--remote-debugging-port=" in args

    async def healthy(self) -> bool:
        if not self.endpoint:
            return False
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                response = await client.get(self.endpoint + "/json/version")
                response.raise_for_status()
                value = response.json()
            websocket = value.get("webSocketDebuggerUrl", "")
            return bool(self.browser_id and websocket.endswith(self.browser_id))
        except (httpx.HTTPError, ValueError):
            return False

    async def ensure_started(self) -> str:
        # Serialize launch/recovery across backend and diagnostic processes.
        # Chrome's profile lock alone does not serialize DevToolsActivePort/state writes.
        self.profile.mkdir(parents=True, exist_ok=True)
        handle = (self.profile / 'jobhunter-launch.lock').open('a+b')
        handle.seek(0)
        if not handle.read(1):
            handle.write(b'0'); handle.flush()
        deadline = time.monotonic() + 25
        acquired = False
        try:
            while not acquired:
                try:
                    handle.seek(0)
                    if sys.platform == 'win32':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise BrowserConnectionError('Another JobHunter process is still initializing the dedicated Chrome profile.')
                    await asyncio.sleep(.1)
            return await self._ensure_started()
        finally:
            if acquired:
                handle.seek(0)
                if sys.platform == 'win32':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    async def _ensure_started(self) -> str:
        async with self._lock:
            if await self.healthy():
                return self.endpoint
            if self.pid and await asyncio.to_thread(self._owned, self.pid):
                # A verified owned process with a dead endpoint cannot be reused.
                await self.shutdown()
            if self.state_file.exists():
                try:
                    record = json.loads(self.state_file.read_text(encoding="utf-8"))
                    pid = int(record["pid"])
                    port = int(record["port"])
                    if not 0 < port < 65536:
                        raise ValueError("invalid port")
                    if not isinstance(record.get('browser_id'), str):
                        raise ValueError('invalid browser identity')
                except (ValueError, KeyError, OSError, TypeError):
                    record = None
                if record and await asyncio.to_thread(self._owned, pid):
                    self.pid = pid
                    self.endpoint = "http://127.0.0.1:%d" % port
                    self.browser_id = record.get("browser_id")
                    if await self.healthy():
                        self.executable = str(find_chrome(self.config.chrome_path))
                        from app.services.diagnostics import emit
                        emit('browser.chrome_reused', **self.payload())
                        return self.endpoint
                    await self.shutdown()
            chrome = find_chrome(self.config.chrome_path)
            self.executable = str(chrome)
            from app.services.diagnostics import emit
            emit('browser.chrome_launch', executable=str(chrome), profile=str(self.profile))
            self.profile.mkdir(parents=True, exist_ok=True)
            active_port = self.profile / "DevToolsActivePort"
            active_port.unlink(missing_ok=True)
            # Chrome requests an OS-allocated free port and writes the exact
            # port/browser identity; there is no bind/release allocation race.
            self.process = subprocess.Popen([
                str(chrome), "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0", "--user-data-dir=%s" % self.profile,
                "--no-first-run", "--no-default-browser-check", "about:blank",
            ])
            self.pid = self.process.pid
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise BrowserConnectionError("Google Chrome exited before CDP startup. Check whether its dedicated profile is already in use.")
                try:
                    lines = active_port.read_text(encoding="utf-8").splitlines()
                    port = int(lines[0])
                    self.browser_id = lines[1]
                    self.endpoint = "http://127.0.0.1:%d" % port
                    if await self.healthy():
                        self.state_file.write_text(json.dumps({"pid": self.pid, "port": port, "browser_id": self.browser_id}), encoding="utf-8")
                        return self.endpoint
                except (OSError, ValueError, IndexError):
                    pass  # Chrome writes this small file asynchronously.
                await asyncio.sleep(0.2)
            await self.shutdown()
            raise BrowserConnectionError("Google Chrome did not expose its CDP endpoint within 20 seconds.")

    async def shutdown(self):
        if self.pid and await asyncio.to_thread(self._owned, self.pid):
            if sys.platform == "win32":
                await asyncio.to_thread(subprocess.run, ["taskkill", "/PID", str(self.pid), "/T", "/F"], capture_output=True)
            elif self.process:
                self.process.terminate()
                await asyncio.to_thread(self.process.wait, timeout=10)
        self.process = None
        self.pid = None
        self.endpoint = None
        self.browser_id = None

    def payload(self):
        return {"mode": "cdp", "pid": self.pid, "endpoint": self.endpoint,
                "executable": self.executable,
                "profile_created_at": self.profile.stat().st_ctime if self.profile.exists() else None,
                "port": int(self.endpoint.rsplit(':', 1)[1]) if self.endpoint else None,
                "profile": str(self.profile), "headed": True}

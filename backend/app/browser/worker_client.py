import asyncio
import json
import sys
from typing import Any, Dict


async def run_browser_self_test(timeout: int = 45) -> Dict[str, Any]:
    """Run Playwright in its own Proactor-configured process and parse the IPC response."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.browser.worker", "--self-test",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return {"ok": False, "error": "Browser worker self-test timed out"}
    raw = stdout.decode("utf-8", errors="replace").strip().splitlines()
    if not raw:
        return {"ok": False, "error": stderr.decode("utf-8", errors="replace")[-2000:] or "Browser worker returned no response"}
    try:
        result = json.loads(raw[-1])
    except json.JSONDecodeError:
        return {"ok": False, "error": "Browser worker returned malformed IPC data", "stdout": raw[-1][-500:]}
    result["exit_code"] = process.returncode
    return result

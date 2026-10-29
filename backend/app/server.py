"""Windows-safe Uvicorn entrypoint. Policy is set before Uvicorn creates its loop."""
import asyncio
import sys
from urllib.parse import urlsplit


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    import uvicorn
    from app.config import settings
    port = urlsplit(settings.backend_url).port or 8000
    host = urlsplit(settings.backend_url).hostname or "127.0.0.1"
    uvicorn.run("app.main:app", host=host, port=port, reload=False, loop="asyncio")


if __name__ == "__main__":
    main()

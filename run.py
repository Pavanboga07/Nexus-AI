"""Development entrypoint.

Run with::

    python run.py

or, for autoreload during development::

    uvicorn app.main:app --reload
"""

from __future__ import annotations

import uvicorn

from app.config.settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.nexus_host,
        port=settings.nexus_port,
        reload=settings.is_development,
        log_level=settings.nexus_log_level.lower(),
    )


if __name__ == "__main__":
    main()

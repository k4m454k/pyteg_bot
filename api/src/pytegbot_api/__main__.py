from __future__ import annotations

import logging

import uvicorn

from pytegbot_api.core.config import get_settings


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        "pytegbot_api.main:app",
        host=settings.server.host,
        port=settings.server.port,
        factory=False,
    )


if __name__ == "__main__":
    main()


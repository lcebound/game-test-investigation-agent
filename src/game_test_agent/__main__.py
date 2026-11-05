from __future__ import annotations

import uvicorn

from game_test_agent.config import Settings
from game_test_agent.observability import configure_logging
from game_test_agent.local_config import load_local_environment


def main() -> None:
    load_local_environment()
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    uvicorn.run(
        "game_test_agent.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
    )


if __name__ == "__main__":
    main()

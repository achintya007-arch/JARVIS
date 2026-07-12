"""
core_v3 entry point.

Run with: python -m core_v3
"""

import asyncio
import logging
import sys
from pathlib import Path

# Windows: UTF-8 stdout/stderr for emoji and unicode.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core.config import Config
from core_v3.brain import Brain

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("jarvis.v3")


async def main() -> None:
    config = Config.load(Path("config.yaml"))
    log.info("JARVIS V3 starting (vault-centric brain)...")
    await Brain(config).start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("JARVIS V3 stopped.")

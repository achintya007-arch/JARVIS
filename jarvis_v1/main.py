"""
Jarvis — Local Multimodal AI Assistant
Entry point (V2 event-driven architecture)
"""

import asyncio
import logging
import sys
from pathlib import Path

# Force UTF-8 stdout/stderr on Windows so emoji and unicode print without crashing.
# Must happen before any other module prints to the terminal.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core.config import Config
from core_v2.brain import Brain

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("jarvis")


async def main():
    config = Config.load(Path("config.yaml"))
    log.info("Jarvis V2 starting...")
    await Brain(config).start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Jarvis stopped.")

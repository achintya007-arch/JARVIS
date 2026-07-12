import asyncio

from core.config import Config
from core_v2.brain import Brain
from core_v2.events import UserInput


# 🔥 Mock minimal config (reuse your real config if possible)
config = Config()

brain = Brain(config)


async def main():
    await brain.start()

    # Simulate user input
    brain._bus.publish(UserInput(text="what time is it", source="test"))

    # wait for system to process
    await asyncio.sleep(5)

    await brain.stop()


asyncio.run(main())
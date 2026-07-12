import asyncio

from core_v2.event_bus import EventBus
from core_v2.events import UserInput

bus = EventBus()

async def handler(event):
    await asyncio.sleep(1)
    print("Finished")

bus.subscribe(UserInput, handler)

async def main():
    print("Waiting...")
    await bus.publish_and_wait(UserInput(text="wait", source="test"))
    print("Done")

asyncio.run(main())

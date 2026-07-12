import asyncio

from core_v2.event_bus import EventBus
from core_v2.events import UserInput

bus = EventBus()

async def handler1(event):
    raise Exception("Test error")

async def handler2(event):
    print(f"[Handler2] Got: {event.text}")
    await asyncio.sleep(0.5)
    print("[Handler2] Done")

bus.subscribe(UserInput, handler1)
bus.subscribe(UserInput, handler2)

async def main():
    print("Publishing event...")
    bus.publish(UserInput(text="Hello Jarvis", source="test"))

    print("Event published (should NOT block)")

    await asyncio.sleep(2)  # wait for handlers to complete

asyncio.run(main())


import asyncio
from core_v2.event_bus import EventBus
from core_v2.events import SpeakRequest

bus = EventBus()

async def speaker(event):
    print(f"[TTS] {event.text}")

bus.subscribe(SpeakRequest, speaker)

async def main():
    bus.publish(SpeakRequest(text="Testing Jarvis", priority=0))
    await asyncio.sleep(1)

asyncio.run(main())
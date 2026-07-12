import asyncio
from core_v2.event_bus import EventBus
from core_v2.events import SpeakRequest, PRIORITY_NORMAL, PRIORITY_CRITICAL
from core_v2.adapters import ActionAdapter

# Mock TTS
class MockTTS:
    async def enqueue(self, text):
        print(f"[TTS enqueue] {text}")

    async def speak(self, text):
        print(f"[TTS speak] {text}")

    async def drain(self):
        print("[TTS drain]")

# Mock Executor
class MockExecutor:
    async def execute(self, tasks):
        return [{"tool": "test", "status": "ok", "result": "done"}]

bus = EventBus()
tts = MockTTS()
executor = MockExecutor()

adapter = ActionAdapter(tts, executor, bus)  # type: ignore

async def main():
    print("Normal request:")
    bus.publish(SpeakRequest(text="Hello", priority=PRIORITY_NORMAL))

    await asyncio.sleep(1)

    print("\nCritical request:")
    bus.publish(SpeakRequest(text="Emergency!", priority=PRIORITY_CRITICAL))

    await asyncio.sleep(1)

asyncio.run(main())
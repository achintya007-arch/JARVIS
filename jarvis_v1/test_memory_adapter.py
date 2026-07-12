import asyncio

from core_v2.adapters import MemoryAdapter
from core_v2.event_bus import EventBus
from core_v2.events import StreamComplete


# Mock ContextManager
class MockContext:
    def __init__(self):
        self.memory_ready = True

    async def add_exchange(self, user, response):
        print(f"[Memory saved] {user} -> {response}")

    async def memory_store_recall(self, text):
        return ["memory1", "memory2"]

    async def build_messages(self, text):
        return [{"role": "user", "content": text}]

bus = EventBus()
context = MockContext()

adapter = MemoryAdapter(context, bus) # type: ignore

async def main():
    bus.publish(StreamComplete(response="Hi there", original_text="Hello"))
    await asyncio.sleep(1)

    mem = await adapter.recall("test")
    print("Recall:", mem)

asyncio.run(main())

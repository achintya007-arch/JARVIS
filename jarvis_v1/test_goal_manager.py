"""
test_goal_manager.py — GoalManager unit tests (offline, no TTS/STT/GPU).
"""
import asyncio, sys, time
sys.path.insert(0, ".")

from cognition.agent_state import AgentState
from cognition.goal_manager import GoalManager
from core_v2.event_bus import EventBus
from core_v2.events import SpeakRequest, UserInput, StreamComplete

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []

def check(label, condition):
    results.append((condition, label))
    print(f"  {PASS if condition else FAIL}  {label}")

async def make_gm(capture_speaks=None):
    bus   = EventBus()
    state = AgentState()
    gm    = GoalManager(db_path=":memory:", agent_state=state, bus=bus)
    if capture_speaks is not None:
        async def _cap(e): capture_speaks.append(e)
        bus.subscribe(SpeakRequest, _cap)
    await gm.initialize()
    return gm, state, bus

async def run():
    print(f"\n{'-'*60}")
    print("  GoalManager Unit Tests")
    print(f"{'-'*60}")

    # 1 — create goal persists to DB
    gm, state, _ = await make_gm()
    await gm.create_goal("study physics", 7, {})
    async with gm._db.execute("SELECT name, status FROM goals WHERE name='study physics'") as c:
        row = await c.fetchone()
    check("create_goal persists to SQLite", row is not None and row[1] == "active")
    await gm.close()

    # 2 — duplicate goal deduplication
    gm, state, _ = await make_gm()
    await gm.create_goal("read books", 5, {})
    await gm.create_goal("read books", 8, {})  # same name, higher priority
    active = [g for g in state.active_goals if g.is_active]
    check("duplicate goal deduplication — only one entry", len(active) == 1)
    check("duplicate goal takes max priority", active[0].priority == 8)
    await gm.close()

    # 3 — complete goal updates DB and state
    gm, state, _ = await make_gm()
    await gm.create_goal("finish report", 6, {})
    done = await gm.complete_goal("finish report")
    check("complete_goal returns True", done)
    check("active_goals pruned after completion", not state.has_active_goals)
    async with gm._db.execute("SELECT status FROM goals WHERE name='finish report'") as c:
        row = await c.fetchone()
    check("complete_goal updates DB status", row and row[0] == "completed")
    await gm.close()

    # 4 — load goals from DB on restart
    import aiosqlite, json, uuid
    db_path = ":memory:"  # can't truly test restart with :memory: so we test _load manually
    gm, state, _ = await make_gm()
    await gm.create_goal("prepare thesis", 7, {})
    # Simulate reload: new state + new gm pointing to same DB object
    state2 = AgentState()
    gm2 = GoalManager.__new__(GoalManager)
    gm2._db_path = ":memory:"
    gm2._state   = state2
    gm2._bus     = EventBus()
    gm2._db      = gm._db   # share same in-memory DB
    gm2._running = False
    await gm2._load_goals_from_db()
    check("load_goals_from_db restores active goals", state2.has_active_goals)
    check("restored goal name matches", state2.top_goal.name == "prepare thesis")
    await gm.close()

    # 5 — detection patterns
    gm, state, _ = await make_gm()
    cases = [
        ("remind me to drink water",          True,  "remind me to"),
        ("I want to finish my thesis",         True,  "I want to"),
        ("set a goal to exercise daily",       True,  "set a goal to"),
        ("my goal is to read 10 books",        True,  "my goal is"),
        ("I have an exam for physics",         True,  "exam for"),
        ("open chrome",                        False, "open chrome — NOT a goal"),
        ("set a timer for 10 minutes",         False, "timer — NOT a goal"),
        ("what time is it",                    False, "time query — NOT a goal"),
    ]
    for text, should_match, label in cases:
        result = gm._detect_goal_creation(text)
        check(f"detect creation: {label}", (result is not None) == should_match)
    await gm.close()

    # 6 — completion detection
    gm, state, _ = await make_gm()
    await gm.create_goal("finish report", 6, {})
    check("detect completion: 'I finished the report'",
          gm._detect_goal_completion("I finished the report") == "finish report")
    check("detect completion: 'done with report'",
          gm._detect_goal_completion("done with the report") == "finish report")
    check("detect completion: 'thanks' → None",
          gm._detect_goal_completion("thanks") is None)
    # Ambiguous "I did it" with exactly one goal
    check("detect completion: ambiguous 'I did it' (1 goal)",
          gm._detect_goal_completion("I did it") == "finish report")
    await gm.close()

    # 7 — reminder respects idle threshold (user active → no reminder)
    spoke = []
    gm, state, bus = await make_gm(spoke)
    await gm.create_goal("study physics", 7, {})
    state.last_interaction_at = time.time()   # user just spoke
    await gm._reminder_tick()
    await asyncio.sleep(0.05)
    check("reminder: no fire when user active", len(spoke) == 0)
    await gm.close()

    # 8 — reminder fires when idle
    spoke = []
    gm, state, bus = await make_gm(spoke)
    await gm.create_goal("study physics", 7, {})
    state.last_interaction_at = time.time() - 300   # idle 5 min
    gm.REMINDER_COOLDOWN = 0   # force eligible
    await gm._reminder_tick()
    await asyncio.sleep(0.05)
    check("reminder: fires when idle", len(spoke) == 1)
    check("reminder: priority=PRIORITY_IMPORTANT", spoke and spoke[0].priority == 5)
    await gm.close()

    # 9 — reminder cooldown respected
    spoke = []
    gm, state, bus = await make_gm(spoke)
    await gm.create_goal("study physics", 7, {})
    state.last_interaction_at = time.time() - 300
    gm.REMINDER_COOLDOWN = 0
    await gm._reminder_tick()   # first — fires
    await gm._reminder_tick()   # second — goal was touched, cooldown=0 so fires again
    await asyncio.sleep(0.05)
    # With REMINDER_COOLDOWN=0 both fire; verify touch() was called (act_count >= 1)
    g = state.top_goal
    check("reminder: touch() called on goal", g and g.act_count >= 1)
    await gm.close()

    # Summary
    total  = len(results)
    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{'-'*60}")
    status = PASS if passed == total else FAIL
    print(f"  {status}  {passed}/{total} passed")
    print(f"{'-'*60}\n")
    sys.exit(0 if passed == total else 1)

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run())
    finally:
        # Cancel any lingering tasks (event bus fire-and-forget tasks)
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

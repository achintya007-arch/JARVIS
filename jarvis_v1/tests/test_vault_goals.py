"""Phase C — GoalManager vault backend (goals persisted as Obsidian tasks)."""

import os
import tempfile

import pytest

pytest.importorskip("aiosqlite")  # goal_manager imports it at module load

from cognition.agent_state import AgentState
from cognition.goal_manager import GoalManager
from core.config import VaultConfig
from core_common.event_bus import EventBus
from memory.vault import Vault


def _vault():
    tmp = tempfile.mkdtemp()
    return Vault(VaultConfig(
        vault_root=os.path.join(tmp, "vault"),
        index_dir=os.path.join(tmp, "idx"),
    ))


async def _manager(vault):
    gm = GoalManager(db_path=None, agent_state=AgentState(), bus=EventBus(), vault=vault)
    await gm.initialize()
    return gm


class TestVaultGoals:
    async def test_create_writes_task_line(self):
        v = _vault()
        gm = await _manager(v)
        await gm.create_goal("finish the report", 8, {})
        text = v.goals_path.read_text(encoding="utf-8")
        assert "- [ ] finish the report" in text
        assert "jarvis" in text  # metadata comment present
        assert gm._state.get_goal("finish the report") is not None
        await gm.close()

    async def test_complete_flips_checkbox(self):
        v = _vault()
        gm = await _manager(v)
        await gm.create_goal("call the dentist", 5, {})
        assert await gm.complete_goal("call the dentist") is True
        text = v.goals_path.read_text(encoding="utf-8")
        assert "- [x] call the dentist" in text
        assert not gm._state.has_active_goals
        await gm.close()

    async def test_drop_removes_line(self):
        v = _vault()
        gm = await _manager(v)
        await gm.create_goal("water the plants", 3, {})
        assert await gm.drop_goal("water the plants") is True
        assert "water the plants" not in v.goals_path.read_text(encoding="utf-8")
        await gm.close()

    async def test_reload_loads_only_active(self):
        v = _vault()
        gm = await _manager(v)
        await gm.create_goal("active goal", 5, {})
        await gm.create_goal("done goal", 5, {})
        await gm.complete_goal("done goal")
        await gm.close()

        # Fresh manager reads goals.md
        gm2 = await _manager(v)
        names = [g.name for g in gm2._state.active_goals]
        assert "active goal" in names
        assert "done goal" not in names
        await gm2.close()

    async def test_handle_input_creates(self):
        v = _vault()
        gm = await _manager(v)
        reply = await gm.handle_input("remind me to submit taxes")
        assert reply is not None and "submit taxes" in reply
        assert any("submit taxes" in g.name for g in gm._state.active_goals)
        await gm.close()

    async def test_handle_input_completes(self):
        v = _vault()
        gm = await _manager(v)
        await gm.create_goal("submit taxes", 5, {})
        reply = await gm.handle_input("I finished submitting taxes")
        assert reply is not None and "complete" in reply.lower()
        assert not gm._state.has_active_goals
        await gm.close()

    async def test_handle_input_declines_non_goal(self):
        # A non-goal utterance returns None so the Brain proceeds to the LLM.
        v = _vault()
        gm = await _manager(v)
        assert await gm.handle_input("what's the weather like") is None
        await gm.close()

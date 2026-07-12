"""Phase C — vault tools: FastRouter routing + ObsidianTools via the executor."""

import os
import tempfile

import pytest

pytest.importorskip("psutil")
pytest.importorskip("httpx")

from core.config import AgentConfig, VaultConfig
from action.fast_router import FastRouter
from action.obsidian_tools import ObsidianTools
from action.agent_executor import AgentExecutor
from memory.vault_memory import VaultMemory


def _executor_with_vault():
    tmp = tempfile.mkdtemp()
    vm = VaultMemory(VaultConfig(
        vault_root=os.path.join(tmp, "vault"),
        index_dir=os.path.join(tmp, "idx"),
    ))
    ex = AgentExecutor(AgentConfig(confirmation_required=False),
                       vault_tools=ObsidianTools(vm))
    return ex, vm


async def _run(ex, tool, args):
    return (await ex.execute([{"tool": tool, "args": args}]))[0]


class TestFastRouterVault:
    def setup_method(self):
        self.r = FastRouter()

    def test_create_note(self):
        res = self.r.route("make a note about the budget meeting")
        assert res.tool == "create_note" and "budget" in res.args["body"]

    def test_search_vault(self):
        res = self.r.route("what do my notes say about physics")
        assert res.tool == "search_vault" and res.args["query"] == "physics"

    def test_search_in_my_notes(self):
        res = self.r.route("search my notes for the roadmap")
        assert res.tool == "search_vault" and "roadmap" in res.args["query"]

    def test_list_today(self):
        assert self.r.route("what did I do today").tool == "list_today"
        assert self.r.route("read my daily note").tool == "list_today"

    def test_append_daily(self):
        res = self.r.route("log this: shipped phase C")
        assert res.tool == "append_to_daily_note" and "shipped" in res.args["text"]

    def test_note_search_not_confused_with_create(self):
        # "search my notes" must route to search, never create_note
        assert self.r.route("search my notes for tacos").tool == "search_vault"


class TestObsidianToolsExecution:
    async def test_create_note_writes_file(self):
        ex, vm = _executor_with_vault()
        r = await _run(ex, "create_note", {"title": "Budget", "body": "Q3 numbers"})
        assert r["status"] == "ok" and "filed a note" in r["result"]
        assert any(p.name == "budget.md" for p in vm.vault.iter_notes())

    async def test_append_and_list_today(self):
        ex, vm = _executor_with_vault()
        await _run(ex, "append_to_daily_note", {"text": "bought milk"})
        assert "bought milk" in vm.vault.read_today()

    async def test_complete_task_toggles_checkbox(self):
        ex, vm = _executor_with_vault()
        vm.vault.goals_path.write_text("- [ ] finish phase C\n", encoding="utf-8")
        r = await _run(ex, "complete_task", {"text": "phase C"})
        assert r["status"] == "ok"
        assert "- [x] finish phase C" in vm.vault.goals_path.read_text(encoding="utf-8")

    async def test_search_vault_graceful_without_embeddings(self):
        ex, _ = _executor_with_vault()
        r = await _run(ex, "search_vault", {"query": "anything"})
        assert r["status"] == "ok" and "nothing" in r["result"].lower()

    async def test_vault_tool_refused_without_vault(self):
        ex = AgentExecutor(AgentConfig(confirmation_required=False))  # no vault_tools
        r = await _run(ex, "create_note", {"title": "x"})
        assert "isn't available" in r["result"]

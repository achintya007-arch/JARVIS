"""Phase D — web RAG stays disabled/safe until the index is built."""

import os
import tempfile

import pytest

pytest.importorskip("httpx")

from core.config import VaultConfig, WebRagConfig
from memory.vault_memory import VaultMemory
from memory.web_rag import WebRAG


class TestWebRagDisabled:
    async def test_disabled_by_config(self):
        wr = WebRAG(WebRagConfig(enabled=False))
        await wr.initialize()
        assert wr.ready is False
        assert await wr.query("anything") == []
        await wr.close()

    async def test_enabled_but_no_index(self, tmp_path):
        cfg = WebRagConfig(enabled=True, index_dir=str(tmp_path / "missing"))
        wr = WebRAG(cfg)
        await wr.initialize()  # index dir doesn't exist → stays disabled
        assert wr.ready is False
        await wr.close()


class TestVaultMemoryIntentGate:
    async def test_no_web_block_when_disabled(self):
        tmp = tempfile.mkdtemp()
        vm = VaultMemory(
            VaultConfig(vault_root=os.path.join(tmp, "vault"),
                        index_dir=os.path.join(tmp, "idx")),
            web_rag_config=WebRagConfig(enabled=False),
        )
        # Even a factual-intent query yields no web-knowledge block (RAG off).
        msgs = await vm.build_messages("what is the boiling point of nitrogen", intent="query")
        assert not any("WEB KNOWLEDGE" in m.get("content", "") for m in msgs)
        assert msgs[-1]["content"].startswith("what is the boiling point")

    async def test_no_web_rag_when_config_absent(self):
        tmp = tempfile.mkdtemp()
        vm = VaultMemory(VaultConfig(vault_root=os.path.join(tmp, "vault"),
                                     index_dir=os.path.join(tmp, "idx")))
        assert vm._web_rag is None
        msgs = await vm.build_messages("factual question", intent="query")
        assert not any("WEB KNOWLEDGE" in m.get("content", "") for m in msgs)

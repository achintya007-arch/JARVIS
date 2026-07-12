"""
Vault memory tests (Phase B) — pure filesystem, no Ollama/Chroma network.

The VaultIndexer is constructed but never initialize()'d, so no embedding calls
are made; semantic recall degrades to empty, exercising the daily-note tier.
"""

from datetime import date

import pytest

from core.config import VaultConfig
from memory.vault import Vault, parse_frontmatter, serialize_frontmatter, slugify
from memory.vault_memory import VaultMemory


def cfg(tmp_path):
    return VaultConfig(
        vault_root=str(tmp_path / "vault"),
        index_dir=str(tmp_path / "idx"),
    )


# ── Frontmatter / slug ────────────────────────────────────────────────────────

class TestFrontmatter:
    def test_roundtrip(self):
        meta = {"title": "Test", "tags": ["jarvis", "note"]}
        body = "# Test\n\nSome body."
        text = serialize_frontmatter(meta, body)
        parsed_meta, parsed_body = parse_frontmatter(text)
        assert parsed_meta == meta
        assert parsed_body.strip() == body

    def test_no_frontmatter(self):
        meta, body = parse_frontmatter("just text, no frontmatter")
        assert meta == {}
        assert body == "just text, no frontmatter"

    def test_malformed_frontmatter_degrades(self):
        meta, body = parse_frontmatter("---\n: : : bad yaml\n---\nbody")
        assert meta == {}

    def test_empty_meta_omits_block(self):
        assert serialize_frontmatter({}, "body") == "body"

    def test_slugify(self):
        assert slugify("My Great Note!") == "my-great-note"
        assert slugify("") == "note"


# ── Vault filesystem ──────────────────────────────────────────────────────────

class TestVault:
    def test_dirs_created(self, tmp_path):
        v = Vault(cfg(tmp_path))
        assert v.jarvis_dir.exists()
        assert v.daily_dir.exists()

    def test_daily_append_and_read(self, tmp_path):
        v = Vault(cfg(tmp_path))
        v.append_exchange("I had pasta for lunch", "Noted, Sir.")
        today = v.read_today()
        assert "pasta" in today
        assert "JARVIS" in today

    def test_daily_path_uses_iso_date(self, tmp_path):
        v = Vault(cfg(tmp_path))
        assert v.daily_path(date(2026, 5, 18)).name == "2026-05-18.md"

    def test_create_note(self, tmp_path):
        v = Vault(cfg(tmp_path))
        p = v.create_note("Project JARVIS", "Uses Ollama and edge-tts.", tags=["proj"])
        assert p.exists()
        text = p.read_text(encoding="utf-8")
        assert "Ollama" in text
        meta, _ = parse_frontmatter(text)
        assert "jarvis" in meta["tags"] and "proj" in meta["tags"]

    def test_toggle_task(self, tmp_path):
        v = Vault(cfg(tmp_path))
        v.goals_path.write_text("- [ ] finish phase B\n- [ ] write tests\n", encoding="utf-8")
        assert v.toggle_task("phase B") is True
        content = v.goals_path.read_text(encoding="utf-8")
        assert "- [x] finish phase B" in content
        assert "- [ ] write tests" in content  # untouched

    def test_toggle_task_no_match(self, tmp_path):
        v = Vault(cfg(tmp_path))
        v.goals_path.write_text("- [ ] something\n", encoding="utf-8")
        assert v.toggle_task("nonexistent") is False

    def test_iter_notes_and_strip_frontmatter(self, tmp_path):
        v = Vault(cfg(tmp_path))
        v.create_note("Note One", "Body of note one.")
        notes = list(v.iter_notes())
        assert any(p.name == "note-one.md" for p in notes)
        target = next(p for p in notes if p.name == "note-one.md")
        body = v.note_text(target)
        assert body.startswith("# Note One")
        assert "---" not in body  # frontmatter stripped

    def test_path_escape_denied(self, tmp_path):
        v = Vault(cfg(tmp_path))
        with pytest.raises(PermissionError):
            v.read("../../etc/passwd")


# ── VaultMemory facade ────────────────────────────────────────────────────────

class TestVaultMemory:
    async def test_add_exchange_persists(self, tmp_path):
        vm = VaultMemory(cfg(tmp_path))
        await vm.add_exchange("I had pasta", "Noted.")
        assert "pasta" in vm.vault.read_today()

    async def test_build_messages_shape(self, tmp_path):
        vm = VaultMemory(cfg(tmp_path))
        await vm.add_exchange("hello", "Hi Sir.")
        msgs = await vm.build_messages("what's up")
        # No embeddings → no memory block; rolling window + user message present.
        assert msgs[-1] == {"role": "user", "content": "what's up"}
        assert {"role": "user", "content": "hello"} in msgs
        assert {"role": "assistant", "content": "Hi Sir."} in msgs

    async def test_rolling_window_seeds_from_daily_note(self, tmp_path):
        c = cfg(tmp_path)
        vm1 = VaultMemory(c)
        await vm1.add_exchange("remember the milk", "I will, Sir.")
        # Fresh instance (simulates restart) re-seeds from today's note.
        vm2 = VaultMemory(c)
        vm2._seed_rolling_window()
        msgs = await vm2.build_messages("did I forget anything")
        assert any(m["content"] == "remember the milk" for m in msgs)

    async def test_ready_without_embeddings(self, tmp_path):
        # Memory is usable (daily-note tier) even when the index isn't up.
        vm = VaultMemory(cfg(tmp_path))
        assert vm.ready is True

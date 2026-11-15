"""
Brain–Body Protocol contract tests: the protocol/ JSON Schemas are valid, every
message the Brain builds validates against them, and malformed messages are
rejected with the right error code. No network, no audio, no Ollama.
"""

import json

import pytest

pytest.importorskip("jsonschema")

from jsonschema import Draft202012Validator

from core_v3.adapters import bbp_protocol as bbp


def _all_schema_files():
    return sorted(bbp.protocol_dir().rglob("*.schema.json"))


# ── The schemas themselves ────────────────────────────────────────────────────

class TestSchemaFiles:
    def test_protocol_dir_found(self):
        assert (bbp.protocol_dir() / "envelope.schema.json").is_file()

    @pytest.mark.parametrize("path", _all_schema_files(), ids=lambda p: p.name)
    def test_schema_is_valid_draft_2020_12(self, path):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))

    def test_every_milestone_1_topic_has_a_schema(self):
        for topic in ("voice.state", "voice.transcript", "reply.chunk", "reply.done", "error"):
            assert bbp.has_schema("event", topic), topic
        for kind in ("hello", "welcome", "ping", "pong"):
            assert bbp.has_schema(kind, kind), kind


# ── Every message the Brain sends ─────────────────────────────────────────────

SNAPSHOT = {
    "voice": {"state": "idle", "muted": False},
    "agent": {"mood": "neutral", "intent": "unknown", "model": "qwen2.5:7b-instruct-q4_K_M",
              "load": {"ram_pct": 71.5, "vram_pct": None}},
    "active_goals": [{"name": "drink water", "priority": 5}],
    "pending_confirmations": [],
}

BRAIN_MESSAGES = [
    bbp.envelope("welcome", "welcome", {"session": "s1", "brain_version": "3.0", "snapshot": SNAPSHOT}),
    bbp.envelope("pong", "pong", {}, corr="ping-1"),
    bbp.envelope("event", "voice.state", {"state": "listening", "muted": False}),
    bbp.envelope("event", "voice.transcript", {"text": "open chrome", "final": True, "confidence": 0.87}),
    bbp.envelope("event", "voice.transcript", {"text": "hi", "final": True, "confidence": None}),
    bbp.envelope("event", "reply.chunk", {"turn_id": "t1", "seq": 0, "text": "Opening Chrome, Sir."}),
    bbp.envelope("event", "reply.done", {"turn_id": "t1", "interrupted": False}),
    bbp.error_response("cmd-1", "E_UNKNOWN_TOPIC", "no handler"),
]


class TestBrainMessagesValidate:
    @pytest.mark.parametrize("message", BRAIN_MESSAGES, ids=lambda m: f"{m['kind']}:{m['topic']}")
    def test_validates(self, message):
        bbp.validate(message)

    def test_envelope_shape(self):
        m = bbp.envelope("event", "voice.state", {"state": "idle", "muted": False})
        assert m["v"] == 1 and m["corr"] is None and len(m["id"]) >= 16
        assert set(m) == {"v", "id", "kind", "topic", "ts", "corr", "payload"}

    def test_ids_are_unique(self):
        assert len({bbp.envelope("ping", "ping", {})["id"] for _ in range(100)}) == 100

    def test_body_hello_validates(self):
        bbp.validate(bbp.envelope("hello", "hello", {
            "client": "win-shell", "version": "0.1.0", "token": "abc",
            "capabilities": ["hud", "notify", "hotkey"],
        }))


# ── Rejections ────────────────────────────────────────────────────────────────

def _code(message):
    with pytest.raises(bbp.BBPError) as e:
        bbp.validate(message)
    return e.value.code


class TestRejections:
    def test_wrong_version(self):
        m = bbp.envelope("ping", "ping", {}); m["v"] = 2
        assert _code(m) == "E_BAD_PAYLOAD"

    def test_missing_field(self):
        m = bbp.envelope("ping", "ping", {}); del m["ts"]
        assert _code(m) == "E_BAD_PAYLOAD"

    def test_session_topic_must_equal_kind(self):
        assert _code(bbp.envelope("ping", "pong", {})) == "E_BAD_PAYLOAD"

    def test_response_requires_corr(self):
        m = bbp.error_response("x", "E_BUSY", "busy"); m["corr"] = None
        assert _code(m) == "E_BAD_PAYLOAD"

    def test_bad_timestamp(self):
        m = bbp.envelope("ping", "ping", {}); m["ts"] = "yesterday"
        assert _code(m) == "E_BAD_PAYLOAD"

    def test_bad_voice_state(self):
        assert _code(bbp.envelope("event", "voice.state", {"state": "dancing", "muted": False})) == "E_BAD_PAYLOAD"

    def test_missing_payload_field(self):
        assert _code(bbp.envelope("event", "reply.chunk", {"turn_id": "t", "text": "x"})) == "E_BAD_PAYLOAD"

    def test_confidence_out_of_range(self):
        m = bbp.envelope("event", "voice.transcript", {"text": "x", "final": True, "confidence": 1.5})
        assert _code(m) == "E_BAD_PAYLOAD"

    def test_unknown_topic(self):
        assert _code(bbp.envelope("event", "sentinel.finding", {})) == "E_UNKNOWN_TOPIC"

    def test_unknown_payload_fields_are_allowed(self):
        # Spec: receivers ignore fields they don't know — adding one never breaks v1.
        bbp.validate(bbp.envelope("event", "voice.state", {"state": "idle", "muted": False, "new": 1}))

    def test_parse_rejects_non_json_and_binary(self):
        with pytest.raises(bbp.BBPError):
            bbp.parse("not json")
        with pytest.raises(bbp.BBPError):
            bbp.parse(b"\x00\x01")


# ── Milestone 2: Body → Brain commands ────────────────────────────────────────

BODY_COMMANDS = [
    bbp.envelope("command", "input.text", {"text": "what's on my calendar"}),
    bbp.envelope("command", "voice.control", {"action": "wake"}),
    bbp.envelope("command", "voice.control", {"action": "mute"}),
    bbp.envelope("command", "voice.control", {"action": "unmute"}),
    bbp.envelope("command", "voice.control", {"action": "cancel"}),
    bbp.envelope("command", "ui.hotkey", {"name": "push_to_talk"}),
    bbp.error_response("cmd-2", "E_BUSY", "JARVIS is speaking; try again in a moment"),
    bbp.error_response("cmd-3", "E_NOT_ALLOWED", "the microphone is muted"),
    bbp.error_response("cmd-4", "E_NOT_FOUND", "no action is mapped to hotkey 'x'"),
]


class TestBodyCommands:
    @pytest.mark.parametrize("message", BODY_COMMANDS, ids=lambda m: f"{m['kind']}:{m['topic']}")
    def test_validates(self, message):
        bbp.validate(message)

    @pytest.mark.parametrize("payload", [{"text": ""}, {"text": "   "}, {"text": "x" * 4001}, {}])
    def test_bad_input_text(self, payload):
        assert _code(bbp.envelope("command", "input.text", payload)) == "E_BAD_PAYLOAD"

    def test_bad_voice_action(self):
        assert _code(bbp.envelope("command", "voice.control", {"action": "dance"})) == "E_BAD_PAYLOAD"

    def test_bad_hotkey_name(self):
        assert _code(bbp.envelope("command", "ui.hotkey", {"name": "Push To Talk"})) == "E_BAD_PAYLOAD"


# ── Milestone 3: notifications and confirmations ──────────────────────────────

PENDING = {"confirm_id": "c1", "prompt": "About to run the command git status. Shall I proceed, Sir?",
           "risk": "high", "timeout_s": 11.5, "detail": "git status"}

MILESTONE_3 = [
    bbp.envelope("event", "notify", {"notify_id": "n1", "level": "warn", "title": "Reminder",
                                     "body": "Sir, a reminder.", "actions": []}),
    bbp.envelope("event", "confirm.request", {**PENDING, "timeout_s": 15}),
    bbp.envelope("event", "confirm.request", {"confirm_id": "c2", "prompt": "Close?", "risk": "low",
                                              "timeout_s": 15}),           # detail is optional
    bbp.envelope("event", "confirm.resolved", {"confirm_id": "c1", "approved": False, "by": "timeout"}),
    bbp.envelope("command", "confirm.response", {"confirm_id": "c1", "approved": True}),
    bbp.envelope("command", "notify.action", {"notify_id": "n1", "action": "snooze"}),
    bbp.envelope("welcome", "welcome", {"session": "s", "brain_version": "3.0",
                                        "snapshot": {**SNAPSHOT, "pending_confirmations": [PENDING]}}),
    bbp.error_response("cmd-5", "E_NOT_FOUND", "that confirmation isn't pending any more"),
]


class TestMilestone3:
    def test_every_topic_has_a_schema(self):
        for topic in ("notify", "confirm.request", "confirm.resolved"):
            assert bbp.has_schema("event", topic), topic
        for topic in ("confirm.response", "notify.action"):
            assert bbp.has_schema("command", topic), topic

    @pytest.mark.parametrize("message", MILESTONE_3, ids=lambda m: f"{m['kind']}:{m['topic']}")
    def test_validates(self, message):
        bbp.validate(message)

    @pytest.mark.parametrize("topic,payload", [
        ("notify", {"notify_id": "n", "level": "panic", "title": "x", "body": "", "actions": []}),
        ("notify", {"notify_id": "n", "level": "info", "title": "", "body": "", "actions": []}),
        ("confirm.request", {**PENDING, "risk": "medium"}),
        ("confirm.request", {**PENDING, "timeout_s": 0}),
        ("confirm.request", {**PENDING, "detail": "x" * 401}),
        ("confirm.resolved", {"confirm_id": "c", "approved": True, "by": "phone"}),
        ("confirm.response", {"confirm_id": "c", "approved": "yes"}),
        ("confirm.response", {"approved": True}),
    ])
    def test_bad_payloads(self, topic, payload):
        kind = "command" if topic == "confirm.response" else "event"
        assert _code(bbp.envelope(kind, topic, payload)) == "E_BAD_PAYLOAD"

"""
Configuration management for Jarvis v1
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


# =========================
# LLM CONFIG
# =========================
@dataclass
class LLMConfig:
    model: str = "qwen2.5:7b-instruct-q4_K_M"
    # A model you actually have pulled — the previous phi3.5 default wasn't
    # installed, so pressure-switching silently no-op'd. qwen2.5:3b is lighter
    # and shares the qwen family, so switches are seamless.
    fallback_model: str = "qwen2.5:3b-instruct-q4_K_M"
    base_url: str = "http://localhost:11434"
    max_tokens: int = 2048
    temperature: float = 0.7
    context_window: int = 4096

    # More relaxed threshold for RTX 4060
    vram_threshold_pct: float = 0.92


# =========================
# SPEECH TO TEXT
# =========================
@dataclass
class STTConfig:
    model: str = "base.en"
    device: str = "cuda"
    compute_type: str = "int8"   # faster + lower VRAM
    language: str = "en"
    vad_filter: bool = True

    # ── Voice-activity capture (command recording) ──────────────────────────
    # These replaced hardcoded constants that made the mic "not listen
    # completely" (cut off on pauses) or "not properly" (missed quiet speech).
    #
    # adaptive_threshold: calibrate the silence threshold from the room's noise
    #   floor at the start of each capture, instead of a fixed RMS that only
    #   works for one mic/environment. silence_threshold is the FLOOR it can't
    #   go below (and the value used when adaptive is off).
    adaptive_threshold: bool = True
    silence_threshold: float = 0.010     # base RMS floor
    onset_multiplier: float = 2.2        # speech starts at noise_floor * this
    end_silence_sec: float = 1.3         # trailing silence that ends a turn (was 0.8)
    max_utterance_sec: float = 15.0      # hard cap per turn (was 8)
    onset_timeout_sec: float = 7.0       # give up if no speech starts after wake
    min_speech_sec: float = 0.3          # ignore blips shorter than this
    pre_speech_sec: float = 0.8          # look-back kept before speech onset

    # ── Transcription confidence gates ──────────────────────────────────────
    # Whisper invents text on silence; these drop low-confidence segments. The
    # previous fixed values dropped real (quiet/accented) speech too.
    no_speech_max: float = 0.6
    avg_logprob_min: float = -1.1

    # Print per-capture diagnostics (calibrated threshold, onset, stop reason,
    # dropped-segment reasons). Turn on when tuning the mic.
    debug_audio: bool = False


# =========================
# TEXT TO SPEECH
# =========================
@dataclass
class TTSConfig:
    engine: str = "piper"
    voice: str = "en_US-ryan-high"
    speed: float = 1.0
    device: str = "cuda"
    # edge-tts prosody — a slightly slower rate reads as composed / deliberate,
    # which suits JARVIS. Format: rate "+/-N%", pitch "+/-NHz".
    rate: str = "-4%"
    pitch: str = "+0Hz"


# =========================
# HOTWORD
# =========================
@dataclass
class HotwordConfig:
    enabled: bool = True
    keyword: str = "jarvis"
    sensitivity: float = 0.5


# =========================
# AGENT
# =========================
@dataclass
class AgentConfig:
    # SECURITY: run_shell is an arbitrary-code-execution surface reachable via
    # LLM tool calls (which can be influenced by injected web/file/clipboard
    # content). It is OFF by default and, when enabled, is constrained to an
    # executable allowlist with shell-metacharacter rejection (see AgentExecutor).
    shell_enabled: bool = False
    file_enabled: bool = True
    web_enabled: bool = True
    confirmation_required: bool = True

    # SECURITY: file tools are confined to this directory. Never None — an
    # unset sandbox previously allowed arbitrary reads (~/.ssh, .env, etc.).
    # Paths outside the sandbox are denied, not silently allowed.
    sandbox_dir: str = "data/workspace"

    # Executables run_shell may invoke (first token of the command). Anything
    # else is denied even when shell_enabled is True. Read-only / inspection
    # commands only by default.
    shell_allowlist: tuple[str, ...] = (
        "echo", "whoami", "hostname", "ver", "date", "time",
        "dir", "ls", "type", "cat", "pwd", "cd", "where", "which",
        "ipconfig", "ping", "systeminfo", "tasklist",
    )

    screen_enabled: bool = True
    screenshot_dir: str = "data/screenshots"


# =========================
# RESOURCE MANAGEMENT
# =========================
@dataclass
class ResourceConfig:
    poll_interval_sec: float = 2.0

    # Less aggressive switching
    max_ram_pct: float = 0.85
    max_vram_mb: float = 6500

    idle_timeout_sec: int = 60


# =========================
# VAULT (V3)
# =========================
@dataclass
class VaultConfig:
    """Configuration for the Obsidian-vault-backed memory layer (V3)."""
    vault_root:   str = "C:/CSE_Projects/JARVIS/vault"
    jarvis_subdir: str = "JARVIS"        # JARVIS-owned area inside the vault
    daily_dir:    str = "daily"          # daily/YYYY-MM-DD.md
    memory_dir:   str = "memory"         # one fact/decision per file
    goals_file:   str = "goals.md"
    index_dir:    str = "data/vault_index"   # Chroma persistence path
    rolling_window_turns: int = 30       # how many recent turns to inline


# =========================
# WEB RAG (V3)
# =========================
@dataclass
class WebRagConfig:
    """Configuration for the FineWeb-derived read-only RAG corpus (V3)."""
    enabled:      bool = False           # Off until Phase D builds the index
    index_dir:    str = "data/web_index"
    embed_model:  str = "nomic-embed-text"
    top_k:        int = 2
    min_score:    float = 0.55           # cosine similarity floor


# =========================
# MAIN CONFIG
# =========================
@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    hotword: HotwordConfig = field(default_factory=HotwordConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    resources: ResourceConfig = field(default_factory=ResourceConfig)
    vault: VaultConfig = field(default_factory=VaultConfig)
    web_rag: WebRagConfig = field(default_factory=WebRagConfig)
    agent_core_max_turns: int = 3
    # STORAGE
    conversation_db: str = "data/conversations.db"
    vector_store_dir: str = "data/chroma"
    log_level: str = "INFO"
    # NEW SYSTEM FLAGS
    voice_enabled: bool = True
    # "wake"    — say "jarvis" to talk; mic muted while JARVIS speaks (default, robust)
    # "voice"   — always-listening (legacy; only sane with headphones)
    # "text"    — keyboard input
    # "hotword" — legacy continuous hotword path
    mode: str = "wake"

    @classmethod
    def load(cls, path: Path) -> Config:
        config = cls()

        if path.exists():
            with path.open() as f:
                data = yaml.safe_load(f) or {}
            for section, values in data.items():
                if hasattr(config, section) and isinstance(values, dict):
                    section_obj = getattr(config, section)
                    for k, v in values.items():
                        if hasattr(section_obj, k):
                            setattr(section_obj, k, v)
                elif hasattr(config, section):
                    setattr(config, section, values)

        # Env override — lets the run script pick a mode without editing config,
        # e.g. JARVIS_MODE=text for keyboard testing with no microphone.
        env_mode = os.environ.get("JARVIS_MODE")
        if env_mode in ("text", "voice", "hotword"):
            config.mode = env_mode

        return config

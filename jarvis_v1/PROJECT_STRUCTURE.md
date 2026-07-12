# Jarvis v1 — Project Structure

jarvis_v1/
├── main.py                        # Entry point
├── config.yaml                    # Runtime config
├── requirements.txt
│
├── core/
│   ├── __init__.py
│   ├── config.py                  # Pydantic/dataclass config
│   └── assistant.py               # Main orchestrator
│
├── perception/
│   ├── __init__.py
│   ├── stt.py                     # faster-whisper STT
│   ├── tts.py                     # Piper / XTTS TTS
│   └── hotword.py                 # pvporcupine wake word
│
├── cognition/
│   ├── __init__.py
│   ├── llm_client.py              # Ollama streaming client
│   ├── context_manager.py         # Conversation history + RAG
│   └── intent_router.py           # Chat vs tool-use classifier
│
├── action/
│   ├── __init__.py
│   ├── agent_executor.py          # Safe tool dispatcher
│   ├── tools/
│   │   ├── shell_tool.py
│   │   ├── file_tool.py
│   │   ├── app_tool.py            # pyautogui integration
│   │   └── web_tool.py            # Playwright integration
│
├── infra/
│   ├── __init__.py
│   ├── resource_monitor.py        # VRAM / RAM watchdog
│   ├── model_loader.py            # Dynamic model management
│   └── event_bus.py               # Internal pub/sub
│
└── data/
    ├── conversations.db           # SQLite conversation history
    └── chroma/                    # Vector store for RAG memory


# requirements.txt
ollama                   # model management CLI / Python client
httpx[asyncio]           # async HTTP for Ollama API
faster-whisper           # GPU STT
piper-tts                # lightweight TTS
pvporcupine              # hotword detection
pyautogui                # desktop automation
playwright               # web browser automation
pywin32                  # Windows OS integration (win32 only)
psutil                   # RAM / CPU monitoring
pynvml                   # NVIDIA VRAM monitoring
chromadb                 # vector store
aiosqlite                # async SQLite
pydantic>=2.0
pyyaml
loguru
asyncio


# config.yaml (example)
llm:
  model: "qwen2.5:7b-instruct-q4_K_M"
  fallback_model: "phi3.5:3.8b-mini-instruct-q4_K_M"
  temperature: 0.7
  context_window: 4096
  vram_threshold_pct: 0.85

stt:
  model: "base.en"
  device: "cuda"
  compute_type: "float16"

tts:
  engine: "piper"
  voice: "en_US-ryan-high"

hotword:
  enabled: true
  keyword: "jarvis"
  sensitivity: 0.5

agent:
  shell_enabled: true
  file_enabled: true
  web_enabled: false
  confirmation_required: true

resources:
  poll_interval_sec: 2.0
  max_vram_mb: 6500
  max_ram_pct: 0.75
  idle_timeout_sec: 60

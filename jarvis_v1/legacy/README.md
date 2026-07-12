# legacy/

Superseded code kept for reference only. **Not imported by the running system.**

## `assistant.py`
The original V1 orchestrator — a single-class, directly-wired assistant
(`Assistant.process_input()`). It was replaced by the event-driven **V2**
orchestrator (`core_v2/brain.py`), and V2 is in turn being superseded by the
vault-centric **V3** orchestrator (`core_v3/brain.py`).

Retained so the evolution of the architecture is legible, but it is not on any
import path and is not covered by tests or CI. Do not build on it.

Run the current system with:

```
python main.py        # V2 (stable)
python -m core_v3      # V3 (in development)
```

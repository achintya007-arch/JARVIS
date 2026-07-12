"""
Shared pytest configuration.

Adds the repository root to sys.path so tests can import the top-level
packages (action, cognition, core, ...) without an editable install. This
keeps the portable unit tests runnable in CI with only pytest installed.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#!/usr/bin/env python3
"""agent-memory entry point. Python 3.8+, standard library only (numpy optional for faster vectors)."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agentmem.cli import main  # noqa: E402

if __name__ == "__main__":
    main()

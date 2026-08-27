import os

from .engine import repair, Result
from .docx import Block, read_blocks, write_docx, blocks_to_html

__all__ = ["repair", "Result", "Block", "read_blocks", "write_docx", "blocks_to_html"]


def _load_dotenv() -> None:
    """Read the first `.env` found walking up from the current directory.

    Dependency-free on purpose: a fresh checkout may not have `python-dotenv`
    installed, and the styling key must still be loadable when the app is run
    directly. Existing environment variables are never overridden.
    """
    from pathlib import Path

    d = Path.cwd()
    for _ in range(6):
        cand = d / ".env"
        if cand.is_file():
            for raw in cand.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = val.strip().strip('"').strip("'")
            return
        if d.parent == d:
            return
        d = d.parent


_load_dotenv()

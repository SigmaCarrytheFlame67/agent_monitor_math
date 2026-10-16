"""How a wrapper receives its task prompt from the console."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence


def read_task_prompt(argv: Sequence[str]) -> str:
    """``--prompt-file PATH`` (the registry's commands), else one argument (an
    operator's command override that still passes ``{prompt}``).

    One argv string is limited to 128 KiB on Linux, so the registry hands the
    prompt over in a file.  The file is deleted once read: the workspace is
    the agent's, and the prompt can carry the operator's agent profile.
    Raises ``OSError`` or ``UnicodeDecodeError`` when the file is unreadable.
    """
    if len(argv) > 2 and argv[1] == "--prompt-file":
        path = Path(argv[2])
        try:
            with path.open(encoding="utf-8", newline="") as handle:
                return handle.read()
        finally:
            path.unlink(missing_ok=True)
    return argv[1] if len(argv) > 1 else ""

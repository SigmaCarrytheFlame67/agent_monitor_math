"""Each turn's proof, kept so the Sandbox can show one proof per turn.

A proof run's ``proof.md`` is rewritten by every turn (the first run, then
each follow-up), so the Sandbox could only ever show the latest one.  Each
turn now leaves a copy of the proof as it was when the turn ended, under
the hidden ``.proof_turns/`` folder of the run's workspace (a dot-folder:
never in the run's file list or export):

- ``.proof_turns/index.json``: ``{"turns": [{"n", "started_at",
  "finished_at", "status", "file", "chars"}]}``, one entry per turn in
  order; a turn still running has ``finished_at`` null and status
  ``running``.
- ``.proof_turns/<n>.md`` (or ``<n>.tex`` for a LaTeX-only proof): the copy.

The console reads the index from the run record (``proof_turns``) and each
copy through the workspace file endpoint.  Everything here is best-effort:
a failure to record a turn never fails the turn itself.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

DIR = ".proof_turns"
INDEX = "index.json"
MAX_TURNS = 200
MAX_CHARS = 400_000

_LOCK = threading.Lock()


def _dir(ws: Path) -> Path:
    return Path(ws) / DIR


def _load(ws: Path) -> list[dict[str, Any]]:
    path = _dir(ws) / INDEX
    try:
        if path.is_symlink() or not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    turns = data.get("turns") if isinstance(data, dict) else None
    return [t for t in turns if isinstance(t, dict)] if isinstance(turns, list) else []


def _folder(ws: Path) -> Path:
    """The turns' folder, made if missing; never one that is a symlink."""
    folder = _dir(ws)
    if folder.is_symlink():
        raise OSError("the turns' folder is a symlink")
    folder.mkdir(exist_ok=True)
    return folder


def _save(ws: Path, turns: list[dict[str, Any]]) -> None:
    folder = _folder(ws)
    tmp = folder / (INDEX + ".tmp")
    tmp.write_text(json.dumps({"turns": turns[-MAX_TURNS:]}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, folder / INDEX)


def begin(ws: Path | str, *, started_at: str) -> int | None:
    """Record a turn starting; returns its number (1 for the first run)."""
    ws = Path(ws)
    try:
        if not ws.is_dir():
            return None
        with _LOCK:
            turns = _load(ws)
            # A turn left running (a restart lost its worker) ended unrecorded.
            for turn in turns:
                if turn.get("finished_at") is None:
                    turn["status"] = "interrupted"
                    turn["finished_at"] = started_at
            n = int(turns[-1].get("n") or len(turns)) + 1 if turns else 1
            turns.append({"n": n, "started_at": started_at, "finished_at": None,
                          "status": "running", "file": "", "chars": 0})
            _save(ws, turns)
            return n
    except (OSError, ValueError, TypeError):
        return None


def end(ws: Path | str, n: int | None, *, status: str, finished_at: str) -> None:
    """Record turn ``n`` ending with ``status``, keeping a copy of its proof."""
    if not n:
        return
    ws = Path(ws)
    try:
        if not ws.is_dir():
            return
        with _LOCK:
            turns = _load(ws)
            turn = next((t for t in turns if t.get("n") == n), None)
            if turn is None or turn.get("finished_at") is not None:
                return  # unknown, or ended already (a turn ends once)
            name, text = "", ""
            for source, suffix in (("proof.md", ".md"), ("proof.tex", ".tex")):
                path = ws / source
                if path.is_file() and not path.is_symlink():
                    text = path.read_text(encoding="utf-8", errors="replace")[:MAX_CHARS]
                    if text.strip():
                        name = f"{n}{suffix}"
                        break
                    text = ""
            if name:
                (_folder(ws) / name).write_text(text, encoding="utf-8")
            turn.update({"finished_at": finished_at, "status": str(status or "finished"),
                         "file": f"{DIR}/{name}" if name else "", "chars": len(text)})
            _save(ws, turns)
    except (OSError, ValueError, TypeError):
        return


def summary(ws: Path | str | None) -> list[dict[str, Any]]:
    """The turns as the console shows them (no proof text)."""
    if not ws:
        return []
    out = []
    for turn in _load(Path(ws)):
        try:
            n = int(turn.get("n"))
        except (TypeError, ValueError):
            continue
        out.append({
            "n": n,
            "started_at": str(turn.get("started_at") or ""),
            "finished_at": turn.get("finished_at") or None,
            "status": str(turn.get("status") or ""),
            "file": str(turn.get("file") or ""),
            "chars": int(turn.get("chars") or 0),
        })
    return out

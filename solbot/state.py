"""Estado del bot (JSON) y diario de órdenes (JSONL, solo se agrega).

Papel y real tienen carpetas separadas. El estado se escribe de forma atómica (archivo
temporal + rename). Si el estado está dañado NO se reinicia en blanco: en modo real eso
haría que el bot volviera a tomar fondos de la wallet como si fuera la primera vez.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class StateError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, folder: str | Path, mode: str):
        self.folder = Path(folder)
        self.mode = mode
        self.path = self.folder / "state.json"
        self.journal_path = self.folder / "journal.jsonl"
        self.lock_path = self.folder / ".lock"

    def default(self) -> dict:
        return {
            "mode": self.mode,
            "running": self.mode == "paper",  # el modo real siempre arranca pausado
            "strategy": None,
            "sleeve": None,
            "start": None,
            "last_bar": None,
            "marks": [],
            "trades": [],
            "pending": None,
            "notice": None,
            "error": None,
            "retry_at": None,
            "attempts": 0,
            "last_cycle": None,
            "outcome": None,
        }

    def load(self) -> dict:
        if not self.path.exists():
            return self.default()
        try:
            data = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            raise StateError(
                f"{self.path} está dañado. No se reinicia solo para no perder el registro de fondos: revísalo o muévelo."
            ) from None
        return {**self.default(), **data}

    def save(self, state: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(f".{os.getpid()}.tmp")
        temp.write_text(json.dumps(state, indent=1, ensure_ascii=False))
        os.replace(temp, self.path)

    def journal(self, event: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        with open(self.journal_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": now_iso(), **event}, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read_journal(self, limit: int = 200) -> list[dict]:
        if not self.journal_path.exists():
            return []
        lines = self.journal_path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in lines if line.strip()]

    @contextmanager
    def lock(self):
        """Un solo ciclo a la vez, aunque el bot corra desde la interfaz y desde cron."""
        self.folder.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

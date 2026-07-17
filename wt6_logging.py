#!/usr/bin/env python3
"""Structured event logging for WT6."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path


class EventLogger:
    """Small JSON-lines event log with day-based retention."""

    LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

    def __init__(self, base_dir: Path, retention_days: int = 14, level: str = "INFO") -> None:
        self.log_dir = Path(base_dir) / "logs"
        self.retention_days = max(1, int(retention_days))
        self.level = level.upper() if level.upper() in self.LEVELS else "INFO"
        self.lock = threading.Lock()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.cleanup_old_logs()

    def cleanup_old_logs(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        for path in self.log_dir.glob("wt6_*.log"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    def log(self, level: str, event: str, **fields: object) -> None:
        level = level.upper()
        if self.LEVELS.get(level, 100) < self.LEVELS.get(self.level, 20):
            return
        now = datetime.now().astimezone()
        record = {
            "time": now.isoformat(timespec="milliseconds"),
            "level": level,
            "event": event,
            **fields,
        }
        path = self.log_dir / f"wt6_{now:%Y-%m-%d}.log"
        with self.lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def debug(self, event: str, **fields: object) -> None:
        self.log("DEBUG", event, **fields)

    def info(self, event: str, **fields: object) -> None:
        self.log("INFO", event, **fields)

    def warn(self, event: str, **fields: object) -> None:
        self.log("WARN", event, **fields)

    def error(self, event: str, **fields: object) -> None:
        self.log("ERROR", event, **fields)





"""简历解析进度文件读写（无重型依赖）。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

PROGRESS_FILENAME = "parse_progress.json"


def progress_path(db_path: Path) -> Path:
    return db_path.parent / PROGRESS_FILENAME


def write_progress(db_path: Path, payload: dict) -> None:
    path = progress_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now().astimezone().isoformat()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def clear_progress(db_path: Path) -> None:
    path = progress_path(db_path)
    if path.exists():
        path.unlink()


def read_progress(db_path: Path) -> dict | None:
    path = progress_path(db_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

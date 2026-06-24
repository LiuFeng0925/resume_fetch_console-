from __future__ import annotations

from pathlib import PurePath


class AttachmentFilter:
    def __init__(self, extensions: list[str]) -> None:
        self._extensions = {
            e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions
        }

    def is_allowed(self, filename: str) -> bool:
        suffix = PurePath(filename).suffix.lower()
        return suffix in self._extensions

from __future__ import annotations

import re


class SubjectMatcher:
    def __init__(self, patterns: list[str], exclude_patterns: list[str] | None = None) -> None:
        self._patterns: list[re.Pattern[str]] = []
        for i, pattern in enumerate(patterns):
            try:
                self._patterns.append(re.compile(pattern))
            except re.error as exc:
                raise ValueError(
                    f"Invalid regex at subject_patterns[{i}]: {pattern}"
                ) from exc

        self._exclude_patterns: list[re.Pattern[str]] = []
        for i, pattern in enumerate(exclude_patterns or []):
            try:
                self._exclude_patterns.append(re.compile(pattern))
            except re.error as exc:
                raise ValueError(
                    f"Invalid regex at subject_exclude_patterns[{i}]: {pattern}"
                ) from exc

    def matches(self, subject: str) -> bool:
        if self._exclude_patterns and any(p.search(subject) for p in self._exclude_patterns):
            return False
        return any(p.search(subject) for p in self._patterns)

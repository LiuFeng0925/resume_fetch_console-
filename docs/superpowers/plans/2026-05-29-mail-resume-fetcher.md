# 邮箱简历附件自动抓取 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python CLI that sequentially processes multiple IMAP accounts from `config.yaml`, downloads whitelisted attachments from subject-matching INBOX emails to per-account folders, deduplicates via SQLite `(account_name, message_id)`, and supports manual `run` plus hourly cron.

**Architecture:** Seven focused modules (`config`, `matcher`, `filter`, `downloader`, `store`, `imap_client`, `orchestrator`) wired by `main.py`. Pure logic modules are unit-tested first (TDD). IMAP is abstracted behind `ImapClient` protocol so orchestrator tests use fakes. First run per account sets UID watermark without backfill.

**Tech Stack:** Python 3.10+, stdlib `imaplib` + `sqlite3` + `argparse` + `logging`, PyYAML, pytest

**Spec sources:** `prd.md`, `config.yaml.example`, `2026-05-29-邮箱简历抓取-mockup.html`

---

## Target File Structure

```
邮箱抓取简历/
├── main.py                      # CLI entry: run [--config] [--account]
├── pyproject.toml               # pytest config, package metadata
├── requirements.txt             # pyyaml, pytest (dev)
├── .gitignore                   # config.yaml, data/, logs/, __pycache__
├── config.yaml.example          # (exists)
├── prd.md                       # (exists)
├── src/
│   ├── __init__.py
│   ├── models.py                # MailMessage, AccountResult, RunSummary dataclasses
│   ├── config.py                # load_config(), validation, env password resolution
│   ├── matcher.py               # SubjectMatcher.matches(subject) -> bool
│   ├── filter.py                # AttachmentFilter.is_allowed(filename) -> bool
│   ├── downloader.py            # AttachmentDownloader.save(...) -> Path
│   ├── store.py                 # ProcessedMailStore (SQLite + watermarks)
│   ├── imap_client.py           # ImapMailClient + FakeImapClient for tests
│   └── orchestrator.py          # run_job(config, account_filter?) -> RunSummary
├── tests/
│   ├── conftest.py              # sample config fixtures, tmp paths
│   ├── test_config.py
│   ├── test_matcher.py
│   ├── test_filter.py
│   ├── test_downloader.py
│   ├── test_store.py
│   └── test_orchestrator.py
├── data/                        # created at runtime; gitignored
└── logs/                        # created at runtime; gitignored
```

**Boundary rules:**
- `orchestrator.py` is the only module that imports all others.
- `imap_client.py` never imports `store` or `downloader`.
- `matcher.py` / `filter.py` have zero I/O.

---

### Task 1: Project Scaffold

**Files:**
- Create: `pyproject.toml`, `requirements.txt`, `.gitignore`, `src/__init__.py`, `tests/conftest.py`

- [ ] **Step 1: Create `.gitignore`**

```gitignore
__pycache__/
*.py[cod]
.pytest_cache/
.venv/
venv/
config.yaml
data/
logs/
*.db
.DS_Store
```

- [ ] **Step 2: Create `requirements.txt`**

```
PyYAML>=6.0
pytest>=8.0
```

- [ ] **Step 3: Create `pyproject.toml`**

```toml
[project]
name = "mail-resume-fetcher"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["PyYAML>=6.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

- [ ] **Step 4: Create empty package files**

`src/__init__.py`:
```python
"""Mail resume attachment fetcher."""
```

`tests/conftest.py`:
```python
import pytest


@pytest.fixture
def sample_subject_patterns():
    return [r".*应聘.*【BOSS直聘】", r".*应聘.*"]
```

- [ ] **Step 5: Install deps and verify pytest runs**

Run: `cd "/Users/liufeng/Documents/项目/重构 2.0/邮箱抓取简历" && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && pytest --collect-only`

Expected: `no tests ran` or empty collection with exit code 0

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml requirements.txt .gitignore src/__init__.py tests/conftest.py
git commit -m "chore: scaffold mail resume fetcher project"
```

---

### Task 2: Shared Models

**Files:**
- Create: `src/models.py`, `tests/test_models.py` (minimal smoke)

- [ ] **Step 1: Write model definitions**

`src/models.py`:
```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: bytes


@dataclass(frozen=True)
class MailMessage:
    uid: int
    message_id: str
    subject: str
    mail_date: datetime
    attachments: tuple[Attachment, ...] = ()


@dataclass
class AccountRunResult:
    account_name: str
    scanned: int = 0
    matched: int = 0
    downloaded: int = 0
    skipped_processed: int = 0
    error: str | None = None


@dataclass
class RunSummary:
    results: list[AccountRunResult] = field(default_factory=list)

    @property
    def any_failed(self) -> bool:
        return any(r.error for r in self.results)

    @property
    def total_downloaded(self) -> int:
        return sum(r.downloaded for r in self.results)
```

- [ ] **Step 2: Smoke test import**

`tests/test_models.py`:
```python
from datetime import datetime, timezone

from src.models import Attachment, MailMessage


def test_mail_message_fields():
    msg = MailMessage(
        uid=1,
        message_id="<a@b>",
        subject="应聘",
        mail_date=datetime(2026, 5, 29, tzinfo=timezone.utc),
        attachments=(Attachment("x.pdf", b"%PDF"),),
    )
    assert msg.attachments[0].filename == "x.pdf"
```

Run: `pytest tests/test_models.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/models.py tests/test_models.py
git commit -m "feat: add shared domain models"
```

---

### Task 3: Subject Matcher (TDD)

**Files:**
- Create: `src/matcher.py`, `tests/test_matcher.py`

- [ ] **Step 1: Write failing tests**

`tests/test_matcher.py`:
```python
import pytest

from src.matcher import SubjectMatcher


@pytest.fixture
def matcher():
    return SubjectMatcher([r".*应聘.*【BOSS直聘】", r".*应聘.*"])


def test_matches_boss_subject(matcher):
    subject = "刘烨 | 7年，应聘 AI产品经理 | 北京30-40K【BOSS直聘】"
    assert matcher.matches(subject) is True


def test_matches_generic_apply(matcher):
    assert matcher.matches("张三应聘Java") is True


def test_no_match_newsletter(matcher):
    assert matcher.matches("公司周报 2026-05") is False


def test_invalid_regex_raises():
    with pytest.raises(ValueError, match="Invalid regex"):
        SubjectMatcher([r"[unclosed"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_matcher.py -v`
Expected: FAIL `ModuleNotFoundError: src.matcher`

- [ ] **Step 3: Implement `SubjectMatcher`**

`src/matcher.py`:
```python
from __future__ import annotations

import re


class SubjectMatcher:
    def __init__(self, patterns: list[str]) -> None:
        self._patterns: list[re.Pattern[str]] = []
        for i, pattern in enumerate(patterns):
            try:
                self._patterns.append(re.compile(pattern))
            except re.error as exc:
                raise ValueError(f"Invalid regex at subject_patterns[{i}]: {pattern}") from exc

    def matches(self, subject: str) -> bool:
        return any(p.search(subject) for p in self._patterns)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_matcher.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/matcher.py tests/test_matcher.py
git commit -m "feat: add subject regex matcher"
```

---

### Task 4: Attachment Filter (TDD)

**Files:**
- Create: `src/filter.py`, `tests/test_filter.py`

- [ ] **Step 1: Write failing tests**

`tests/test_filter.py`:
```python
from src.filter import AttachmentFilter


def test_allows_pdf_case_insensitive():
    f = AttachmentFilter([".pdf"])
    assert f.is_allowed("resume.PDF") is True
    assert f.is_allowed("resume.pdf") is True


def test_rejects_other_extensions():
    f = AttachmentFilter([".pdf"])
    assert f.is_allowed("notes.docx") is False


def test_no_extension_rejected():
    f = AttachmentFilter([".pdf"])
    assert f.is_allowed("README") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_filter.py -v`
Expected: FAIL import error

- [ ] **Step 3: Implement**

`src/filter.py`:
```python
from __future__ import annotations

from pathlib import PurePath


class AttachmentFilter:
    def __init__(self, extensions: list[str]) -> None:
        self._extensions = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}

    def is_allowed(self, filename: str) -> bool:
        suffix = PurePath(filename).suffix.lower()
        return suffix in self._extensions
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_filter.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/filter.py tests/test_filter.py
git commit -m "feat: add attachment extension filter"
```

---

### Task 5: Attachment Downloader (TDD)

**Files:**
- Create: `src/downloader.py`, `tests/test_downloader.py`

- [ ] **Step 1: Write failing tests**

`tests/test_downloader.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

from src.downloader import AttachmentDownloader


def test_save_uses_date_prefix_and_original_name(tmp_path):
    dl = AttachmentDownloader(tmp_path)
    mail_date = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    path = dl.save(
        mail_date=mail_date,
        original_filename="【AI产品经理_北京_30-40K】刘烨_7年.pdf",
        content=b"%PDF-1.4",
    )
    assert path.name == "20260529_【AI产品经理_北京_30-40K】刘烨_7年.pdf"
    assert path.read_bytes() == b"%PDF-1.4"


def test_collision_appends_suffix(tmp_path):
    dl = AttachmentDownloader(tmp_path)
    mail_date = datetime(2026, 5, 29, tzinfo=timezone.utc)
    dl.save(mail_date, "resume.pdf", b"1")
    path2 = dl.save(mail_date, "resume.pdf", b"2")
    assert path2.name == "20260529_resume_2.pdf"


def test_sanitize_path_separators(tmp_path):
    dl = AttachmentDownloader(tmp_path)
    mail_date = datetime(2026, 5, 29, tzinfo=timezone.utc)
    path = dl.save(mail_date, "bad/name:file.pdf", b"x")
    assert "/" not in path.name
    assert ":" not in path.name
    assert path.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_downloader.py -v`
Expected: FAIL import error

- [ ] **Step 3: Implement**

`src/downloader.py`:
```python
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


class AttachmentDownloader:
    def __init__(self, download_dir: Path) -> None:
        self._download_dir = download_dir
        self._download_dir.mkdir(parents=True, exist_ok=True)

    def save(self, mail_date: datetime, original_filename: str, content: bytes) -> Path:
        safe_name = self._sanitize_filename(original_filename)
        date_prefix = mail_date.strftime("%Y%m%d")
        candidate = f"{date_prefix}_{safe_name}"
        path = self._download_dir / candidate
        path = self._resolve_collision(path)
        path.write_bytes(content)
        return path

    def _sanitize_filename(self, filename: str) -> str:
        # macOS: disallow / and : in filenames
        name = filename.replace("/", "_").replace(":", "_")
        name = name.strip() or "attachment"
        return name

    def _resolve_collision(self, path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        n = 2
        while True:
            candidate = path.with_name(f"{stem}_{n}{suffix}")
            if not candidate.exists():
                return candidate
            n += 1
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_downloader.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/downloader.py tests/test_downloader.py
git commit -m "feat: add attachment downloader with naming and collision handling"
```

---

### Task 6: Processed Mail Store (TDD)

**Files:**
- Create: `src/store.py`, `tests/test_store.py`

- [ ] **Step 1: Write failing tests**

`tests/test_store.py`:
```python
from datetime import datetime, timezone

from src.store import ProcessedMailStore


def test_is_processed_false_initially(tmp_path):
    store = ProcessedMailStore(tmp_path / "test.db")
    assert store.is_processed("hr-boss", "<id@example.com>") is False


def test_mark_and_check_processed(tmp_path):
    store = ProcessedMailStore(tmp_path / "test.db")
    store.mark_processed(
        account_name="hr-boss",
        message_id="<id@example.com>",
        subject="应聘",
        mail_date=datetime(2026, 5, 29, tzinfo=timezone.utc),
        saved_files=["/tmp/a.pdf"],
        matched=True,
    )
    assert store.is_processed("hr-boss", "<id@example.com>") is True


def test_dedup_scoped_by_account(tmp_path):
    store = ProcessedMailStore(tmp_path / "test.db")
    mid = "<same@example.com>"
    store.mark_processed("acc-a", mid, "s", datetime.now(timezone.utc), [], True)
    assert store.is_processed("acc-a", mid) is True
    assert store.is_processed("acc-b", mid) is False


def test_watermark_first_run_none_then_set(tmp_path):
    store = ProcessedMailStore(tmp_path / "test.db")
    assert store.get_watermark_uid("hr-boss") is None
    store.set_watermark_uid("hr-boss", 42)
    assert store.get_watermark_uid("hr-boss") == 42
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_store.py -v`
Expected: FAIL import error

- [ ] **Step 3: Implement**

`src/store.py`:
```python
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path


class ProcessedMailStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed_emails (
                account_name TEXT NOT NULL,
                message_id   TEXT NOT NULL,
                subject      TEXT,
                mail_date    TEXT,
                processed_at TEXT NOT NULL,
                saved_files  TEXT NOT NULL,
                matched      INTEGER NOT NULL,
                PRIMARY KEY (account_name, message_id)
            );
            CREATE TABLE IF NOT EXISTS watermarks (
                account_name TEXT PRIMARY KEY,
                since_uid    INTEGER NOT NULL
            );
            """
        )
        self._conn.commit()

    def is_processed(self, account_name: str, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_emails WHERE account_name = ? AND message_id = ?",
            (account_name, message_id),
        ).fetchone()
        return row is not None

    def mark_processed(
        self,
        account_name: str,
        message_id: str,
        subject: str,
        mail_date: datetime,
        saved_files: list[str],
        matched: bool,
    ) -> None:
        processed_at = datetime.now().astimezone().isoformat()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO processed_emails
            (account_name, message_id, subject, mail_date, processed_at, saved_files, matched)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_name,
                message_id,
                subject,
                mail_date.astimezone().isoformat(),
                processed_at,
                json.dumps(saved_files, ensure_ascii=False),
                1 if matched else 0,
            ),
        )
        self._conn.commit()

    def get_watermark_uid(self, account_name: str) -> int | None:
        row = self._conn.execute(
            "SELECT since_uid FROM watermarks WHERE account_name = ?",
            (account_name,),
        ).fetchone()
        return int(row["since_uid"]) if row else None

    def set_watermark_uid(self, account_name: str, since_uid: int) -> None:
        self._conn.execute(
            """
            INSERT INTO watermarks (account_name, since_uid) VALUES (?, ?)
            ON CONFLICT(account_name) DO UPDATE SET since_uid = excluded.since_uid
            """,
            (account_name, since_uid),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_store.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/store.py tests/test_store.py
git commit -m "feat: add SQLite store with per-account dedup and UID watermarks"
```

---

### Task 7: Config Loader (TDD)

**Files:**
- Create: `src/config.py`, `tests/test_config.py`, `tests/fixtures/minimal_config.yaml`

- [ ] **Step 1: Create test fixture**

`tests/fixtures/minimal_config.yaml`:
```yaml
accounts:
  - name: hr-boss
    imap:
      host: imap.example.com
      port: 993
      ssl: true
      username: hr@example.com
      password_env: IMAP_PASSWORD_HR
    mailbox: INBOX
    download:
      path: /tmp/resumes/hr
  - name: recruiter-2
    imap:
      host: imap.example.com
      port: 993
      ssl: true
      username: r2@example.com
      password_env: IMAP_PASSWORD_R2
    mailbox: INBOX
    download:
      path: /tmp/resumes/r2
subject_patterns:
  - ".*应聘.*"
attachment_extensions:
  - ".pdf"
state:
  db_path: ./data/processed.db
log:
  path: ./logs/resume-fetch.log
```

- [ ] **Step 2: Write failing tests**

`tests/test_config.py`:
```python
from pathlib import Path

import pytest

from src.config import ConfigError, load_config


@pytest.fixture
def minimal_config_path():
    return Path(__file__).parent / "fixtures" / "minimal_config.yaml"


def test_load_config_parses_accounts(minimal_config_path, monkeypatch):
    monkeypatch.setenv("IMAP_PASSWORD_HR", "secret1")
    monkeypatch.setenv("IMAP_PASSWORD_R2", "secret2")
    cfg = load_config(minimal_config_path)
    assert len(cfg.accounts) == 2
    assert cfg.accounts[0].name == "hr-boss"
    assert cfg.accounts[0].password == "secret1"
    assert cfg.accounts[1].password == "secret2"


def test_missing_password_env_raises(minimal_config_path, monkeypatch):
    monkeypatch.delenv("IMAP_PASSWORD_HR", raising=False)
    monkeypatch.setenv("IMAP_PASSWORD_R2", "secret2")
    with pytest.raises(ConfigError, match="IMAP_PASSWORD_HR"):
        load_config(minimal_config_path)


def test_duplicate_account_names_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAP_PASSWORD_HR", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
accounts:
  - name: dup
    imap: {host: h, port: 993, ssl: true, username: a, password_env: IMAP_PASSWORD_HR}
    download: {path: /tmp/a}
  - name: dup
    imap: {host: h, port: 993, ssl: true, username: b, password_env: IMAP_PASSWORD_HR}
    download: {path: /tmp/b}
subject_patterns: [".*"]
attachment_extensions: [".pdf"]
state: {db_path: ./data/db}
log: {path: ./logs/x.log}
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(bad)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_config.py -v`
Expected: FAIL import error

- [ ] **Step 4: Implement**

`src/config.py`:
```python
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ImapSettings:
    host: str
    port: int
    ssl: bool
    username: str
    password_env: str


@dataclass(frozen=True)
class AccountConfig:
    name: str
    imap: ImapSettings
    mailbox: str
    download_path: Path
    password: str  # resolved at load time


@dataclass(frozen=True)
class AppConfig:
    accounts: tuple[AccountConfig, ...]
    subject_patterns: tuple[str, ...]
    attachment_extensions: tuple[str, ...]
    db_path: Path
    log_path: Path


def load_config(path: Path) -> AppConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"Invalid config root in {path}")

    accounts_raw = raw.get("accounts")
    if not accounts_raw or not isinstance(accounts_raw, list):
        raise ConfigError("config must contain non-empty accounts list")

    seen_names: set[str] = set()
    accounts: list[AccountConfig] = []
    for item in accounts_raw:
        name = item.get("name")
        if not name:
            raise ConfigError("each account requires name")
        if name in seen_names:
            raise ConfigError(f"duplicate account name: {name}")
        seen_names.add(name)

        imap_raw = item.get("imap") or {}
        password_env = imap_raw.get("password_env")
        if not password_env:
            raise ConfigError(f"account {name}: imap.password_env required")
        password = os.environ.get(password_env)
        if not password:
            raise ConfigError(
                f"account {name}: environment variable {password_env} is not set"
            )

        download_raw = item.get("download") or {}
        download_path = download_raw.get("path")
        if not download_path:
            raise ConfigError(f"account {name}: download.path required")

        accounts.append(
            AccountConfig(
                name=name,
                imap=ImapSettings(
                    host=imap_raw["host"],
                    port=int(imap_raw.get("port", 993)),
                    ssl=bool(imap_raw.get("ssl", True)),
                    username=imap_raw["username"],
                    password_env=password_env,
                ),
                mailbox=item.get("mailbox", "INBOX"),
                download_path=Path(download_path),
                password=password,
            )
        )

    patterns = tuple(raw.get("subject_patterns") or [])
    if not patterns:
        raise ConfigError("subject_patterns must be non-empty")

    extensions = tuple(raw.get("attachment_extensions") or [])
    if not extensions:
        raise ConfigError("attachment_extensions must be non-empty")

    state = raw.get("state") or {}
    log = raw.get("log") or {}
    config_dir = path.parent.resolve()

    db_path = Path(state.get("db_path", "./data/processed.db"))
    log_path = Path(log.get("log_path", log.get("path", "./logs/resume-fetch.log")))
    if not log_path.is_absolute():
        log_path = config_dir / log_path
    if not db_path.is_absolute():
        db_path = config_dir / db_path

    return AppConfig(
        accounts=tuple(accounts),
        subject_patterns=patterns,
        attachment_extensions=extensions,
        db_path=db_path,
        log_path=log_path,
    )
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_config.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add src/config.py tests/test_config.py tests/fixtures/minimal_config.yaml
git commit -m "feat: add multi-account YAML config loader"
```

---

### Task 8: IMAP Client

**Files:**
- Create: `src/imap_client.py`, `tests/test_imap_client.py` (unit tests for parsing helpers only; live IMAP optional manual)

- [ ] **Step 1: Write parser unit tests (no network)**

`tests/test_imap_client.py`:
```python
from email.message import EmailMessage

from src.imap_client import extract_attachments, extract_message_id, parse_mail_message


def test_extract_message_id_from_header():
    msg = EmailMessage()
    msg["Message-ID"] = "<abc@boss.com>"
    assert extract_message_id(msg) == "<abc@boss.com>"


def test_extract_message_id_missing_generates_stable_fallback():
    msg = EmailMessage()
    assert extract_message_id(msg, uid=99).endswith("@local.generated>")


def test_extract_attachments_skips_inline():
    msg = EmailMessage()
    msg["Subject"] = "应聘"
    msg.set_type("multipart/mixed")
    part = EmailMessage()
    part.add_header("Content-Disposition", "attachment", filename="cv.pdf")
    part.set_payload(b"%PDF")
    msg.attach(part)
    inline = EmailMessage()
    inline.add_header("Content-Disposition", "inline")
    inline.set_payload(b"html")
    msg.attach(inline)
    attachments = extract_attachments(msg)
    assert len(attachments) == 1
    assert attachments[0].filename == "cv.pdf"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_imap_client.py -v`
Expected: FAIL import error

- [ ] **Step 3: Implement parsing helpers + `ImapMailClient`**

`src/imap_client.py` (core parts — full file in repo):
```python
from __future__ import annotations

import email
import imaplib
from datetime import datetime, timezone
from email.message import Message
from typing import Protocol

from src.models import Attachment, MailMessage


def extract_message_id(msg: Message, uid: int | None = None) -> str:
    mid = msg.get("Message-ID")
    if mid:
        return mid.strip()
    return f"<uid-{uid}@local.generated>" if uid is not None else "<unknown@local.generated>"


def extract_attachments(msg: Message) -> list[Attachment]:
    results: list[Attachment] = []
    if not msg.is_multipart():
        return results
    for part in msg.walk():
        if part.get_content_disposition() != "attachment":
            continue
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        results.append(Attachment(filename=filename, content=payload))
    return results


def parse_mail_message(uid: int, raw_bytes: bytes) -> MailMessage:
    msg = email.message_from_bytes(raw_bytes)
    subject = msg.get("Subject", "") or ""
    date_hdr = msg.get("Date")
    if date_hdr:
        mail_date = email.utils.parsedate_to_datetime(date_hdr)
        if mail_date.tzinfo is None:
            mail_date = mail_date.replace(tzinfo=timezone.utc)
    else:
        mail_date = datetime.now(timezone.utc)
    return MailMessage(
        uid=uid,
        message_id=extract_message_id(msg, uid=uid),
        subject=subject,
        mail_date=mail_date,
        attachments=tuple(extract_attachments(msg)),
    )


class MailFetcher(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def get_max_uid(self) -> int: ...
    def fetch_messages_after_uid(self, after_uid: int) -> list[MailMessage]: ...


class ImapMailClient:
    def __init__(
        self,
        host: str,
        port: int,
        ssl: bool,
        username: str,
        password: str,
        mailbox: str = "INBOX",
    ) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._username = username
        self._password = password
        self._mailbox = mailbox
        self._conn: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None

    def connect(self) -> None:
        if self._ssl:
            self._conn = imaplib.IMAP4_SSL(self._host, self._port)
        else:
            self._conn = imaplib.IMAP4(self._host, self._port)
        assert self._conn is not None
        self._conn.login(self._username, self._password)
        status, _ = self._conn.select(self._mailbox, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Cannot select mailbox {self._mailbox}")

    def disconnect(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except imaplib.IMAP4.error:
                pass
            try:
                self._conn.logout()
            except imaplib.IMAP4.error:
                pass
            self._conn = None

    def get_max_uid(self) -> int:
        assert self._conn is not None
        status, data = self._conn.uid("SEARCH", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return 0
        uids = [int(x) for x in data[0].split()]
        return max(uids) if uids else 0

    def fetch_messages_after_uid(self, after_uid: int) -> list[MailMessage]:
        assert self._conn is not None
        if after_uid <= 0:
            criteria = "ALL"
        else:
            criteria = f"UID {after_uid + 1}:*"
        status, data = self._conn.uid("SEARCH", None, criteria)
        if status != "OK" or not data or not data[0]:
            return []
        uids = [int(x) for x in data[0].split()]
        messages: list[MailMessage] = []
        for uid in uids:
            st, fetched = self._conn.uid("FETCH", str(uid), "(RFC822)")
            if st != "OK" or not fetched:
                continue
            raw = fetched[0][1]
            if isinstance(raw, bytes):
                messages.append(parse_mail_message(uid, raw))
        return messages
```

- [ ] **Step 4: Run parser tests**

Run: `pytest tests/test_imap_client.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/imap_client.py tests/test_imap_client.py
git commit -m "feat: add IMAP client with attachment parsing"
```

---

### Task 9: Job Orchestrator (TDD with Fake IMAP)

**Files:**
- Create: `src/orchestrator.py`, `tests/test_orchestrator.py`

- [ ] **Step 1: Write fake fetcher + failing orchestrator tests**

Add to `tests/test_orchestrator.py`:
```python
from datetime import datetime, timezone

import pytest

from src.config import AccountConfig, AppConfig, ImapSettings
from src.models import Attachment, MailMessage
from src.orchestrator import run_job
from src.store import ProcessedMailStore


class FakeFetcher:
    def __init__(self, messages: list[MailMessage], max_uid: int = 10):
        self._messages = messages
        self._max_uid = max_uid
        self.connected = False

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def get_max_uid(self) -> int:
        return self._max_uid

    def fetch_messages_after_uid(self, after_uid: int) -> list[MailMessage]:
        return [m for m in self._messages if m.uid > after_uid]


def _app_config(tmp_path) -> AppConfig:
    acc = AccountConfig(
        name="hr-boss",
        imap=ImapSettings("h", 993, True, "u", "IMAP_PASSWORD_HR"),
        mailbox="INBOX",
        download_path=tmp_path / "hr",
        password="pw",
    )
    return AppConfig(
        accounts=(acc,),
        subject_patterns=(r".*应聘.*",),
        attachment_extensions=(".pdf",),
        db_path=tmp_path / "data.db",
        log_path=tmp_path / "run.log",
    )


def test_first_run_sets_watermark_without_download(tmp_path):
    cfg = _app_config(tmp_path)
    store = ProcessedMailStore(cfg.db_path)
    msg = MailMessage(
        uid=5,
        message_id="<old@x>",
        subject="应聘 BOSS",
        mail_date=datetime.now(timezone.utc),
        attachments=(Attachment("cv.pdf", b"%PDF"),),
    )
    fetcher = FakeFetcher([msg], max_uid=5)

    def factory(account):
        return fetcher

    summary = run_job(cfg, store=store, fetcher_factory=factory)
    assert summary.results[0].downloaded == 0
    assert store.get_watermark_uid("hr-boss") == 5
    store.close()


def test_second_run_downloads_new_mail(tmp_path):
    cfg = _app_config(tmp_path)
    store = ProcessedMailStore(cfg.db_path)
    store.set_watermark_uid("hr-boss", 5)
    msg = MailMessage(
        uid=6,
        message_id="<new@x>",
        subject="刘烨 | 应聘 AI产品经理【BOSS直聘】",
        mail_date=datetime(2026, 5, 29, tzinfo=timezone.utc),
        attachments=(Attachment("【AI产品经理_北京_30-40K】刘烨_7年.pdf", b"%PDF"),),
    )
    fetcher = FakeFetcher([msg], max_uid=6)

    summary = run_job(cfg, store=store, fetcher_factory=lambda a: fetcher)
    assert summary.results[0].downloaded == 1
    saved = list((tmp_path / "hr").glob("*.pdf"))
    assert len(saved) == 1
    assert saved[0].name.startswith("20260529_")
    store.close()


def test_non_matching_subject_still_marked_processed(tmp_path):
    cfg = _app_config(tmp_path)
    store = ProcessedMailStore(cfg.db_path)
    store.set_watermark_uid("hr-boss", 0)
    msg = MailMessage(
        uid=1,
        message_id="<news@x>",
        subject="公司周报",
        mail_date=datetime.now(timezone.utc),
        attachments=(),
    )
    summary = run_job(
        cfg,
        store=store,
        fetcher_factory=lambda a: FakeFetcher([msg], max_uid=1),
    )
    assert summary.results[0].downloaded == 0
    assert store.is_processed("hr-boss", "<news@x>")
    store.close()


def test_one_account_failure_continues_other(tmp_path):
    acc1 = AccountConfig(
        name="ok",
        imap=ImapSettings("h", 993, True, "u1", "E1"),
        mailbox="INBOX",
        download_path=tmp_path / "ok",
        password="pw",
    )
    acc2 = AccountConfig(
        name="bad",
        imap=ImapSettings("h", 993, True, "u2", "E2"),
        mailbox="INBOX",
        download_path=tmp_path / "bad",
        password="pw",
    )
    cfg = AppConfig(
        accounts=(acc1, acc2),
        subject_patterns=(r".*应聘.*",),
        attachment_extensions=(".pdf",),
        db_path=tmp_path / "data.db",
        log_path=tmp_path / "run.log",
    )
    store = ProcessedMailStore(cfg.db_path)
    store.set_watermark_uid("ok", 0)

    class FailFetcher:
        def connect(self):
            raise RuntimeError("AUTH FAILED")
        def disconnect(self): ...
        def get_max_uid(self): return 0
        def fetch_messages_after_uid(self, after_uid): return []

    def factory(account):
        if account.name == "bad":
            return FailFetcher()
        return FakeFetcher([], max_uid=0)

    summary = run_job(cfg, store=store, fetcher_factory=factory)
    assert len(summary.results) == 2
    assert summary.results[0].error is None
    assert summary.results[1].error is not None
    assert summary.any_failed is True
    store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_orchestrator.py -v`
Expected: FAIL import error

- [ ] **Step 3: Implement orchestrator**

`src/orchestrator.py`:
```python
from __future__ import annotations

import logging
from typing import Callable

from src.config import AccountConfig, AppConfig
from src.downloader import AttachmentDownloader
from src.filter import AttachmentFilter
from src.imap_client import ImapMailClient, MailFetcher
from src.matcher import SubjectMatcher
from src.models import AccountRunResult, RunSummary
from src.store import ProcessedMailStore

logger = logging.getLogger(__name__)

FetcherFactory = Callable[[AccountConfig], MailFetcher]


def run_job(
    config: AppConfig,
    store: ProcessedMailStore,
    fetcher_factory: FetcherFactory | None = None,
    account_filter: str | None = None,
) -> RunSummary:
    matcher = SubjectMatcher(list(config.subject_patterns))
    attachment_filter = AttachmentFilter(list(config.attachment_extensions))
    summary = RunSummary()

    accounts = config.accounts
    if account_filter:
        accounts = tuple(a for a in accounts if a.name == account_filter)
        if not accounts:
            raise ValueError(f"Unknown account: {account_filter}")

    for account in accounts:
        result = AccountRunResult(account_name=account.name)
        fetcher = (
            fetcher_factory(account)
            if fetcher_factory
            else ImapMailClient(
                host=account.imap.host,
                port=account.imap.port,
                ssl=account.imap.ssl,
                username=account.imap.username,
                password=account.password,
                mailbox=account.mailbox,
            )
        )
        try:
            fetcher.connect()
            watermark = store.get_watermark_uid(account.name)
            if watermark is None:
                max_uid = fetcher.get_max_uid()
                store.set_watermark_uid(account.name, max_uid)
                logger.info(
                    "%s: first run, watermark set to UID %s (no backfill)",
                    account.name,
                    max_uid,
                )
                fetcher.disconnect()
                summary.results.append(result)
                continue

            messages = fetcher.fetch_messages_after_uid(watermark)
            downloader = AttachmentDownloader(account.download_path)
            max_seen_uid = watermark

            for msg in messages:
                result.scanned += 1
                max_seen_uid = max(max_seen_uid, msg.uid)
                if store.is_processed(account.name, msg.message_id):
                    result.skipped_processed += 1
                    continue

                saved_paths: list[str] = []
                if matcher.matches(msg.subject):
                    result.matched += 1
                    for att in msg.attachments:
                        if not attachment_filter.is_allowed(att.filename):
                            continue
                        path = downloader.save(msg.mail_date, att.filename, att.content)
                        saved_paths.append(str(path))
                        result.downloaded += 1

                store.mark_processed(
                    account_name=account.name,
                    message_id=msg.message_id,
                    subject=msg.subject,
                    mail_date=msg.mail_date,
                    saved_files=saved_paths,
                    matched=bool(saved_paths) or matcher.matches(msg.subject),
                )

            store.set_watermark_uid(account.name, max_seen_uid)
            fetcher.disconnect()
        except Exception as exc:
            logger.exception("%s: account run failed", account.name)
            result.error = str(exc)
            try:
                fetcher.disconnect()
            except Exception:
                pass
        summary.results.append(result)

    return summary
```

- [ ] **Step 4: Run orchestrator tests**

Run: `pytest tests/test_orchestrator.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: add multi-account job orchestrator with watermark backfill skip"
```

---

### Task 10: CLI Entry Point (`main.py`)

**Files:**
- Create: `main.py`, `tests/test_cli.py`

- [ ] **Step 1: Write CLI smoke test**

`tests/test_cli.py`:
```python
import subprocess
import sys
from pathlib import Path


def test_main_help():
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "main.py", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "run" in proc.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL (no main.py)

- [ ] **Step 3: Implement `main.py`**

`main.py`:
```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.config import ConfigError, load_config
from src.orchestrator import run_job
from src.store import ProcessedMailStore


def _setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _print_summary(summary) -> None:
    total_downloaded = 0
    failed = 0
    for r in summary.results:
        if r.error:
            print(f"{r.account_name}: FAILED — {r.error}")
            failed += 1
        else:
            print(
                f"{r.account_name}: scanned={r.scanned} matched={r.matched} "
                f"downloaded={r.downloaded} skipped_processed={r.skipped_processed}"
            )
            total_downloaded += r.downloaded
    print(
        f"汇总: {len(summary.results)} 账号, {failed} 失败, "
        f"共下载 {total_downloaded} 个附件"
    )


def cmd_run(config_path: Path, account: str | None) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1

    _setup_logging(config.log_path)
    store = ProcessedMailStore(config.db_path)
    try:
        summary = run_job(config, store=store, account_filter=account)
    finally:
        store.close()

    _print_summary(summary)
    return 1 if summary.any_failed else 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="邮箱简历附件自动抓取")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="配置文件路径 (default: config.yaml)",
    )
    sub = parser.add_subparsers(dest="command")
    run_parser = sub.add_parser("run", help="立即执行一轮抓取")
    run_parser.add_argument("--account", help="仅处理指定 account name")

    if not argv:
        return cmd_run(Path("config.yaml"), account=None)

    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args.config, account=args.account)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run CLI test + full suite**

Run: `pytest tests/ -v`
Expected: all tests PASS

Run: `python main.py --help`
Expected: shows `run`, `--account`, `--config`

Run: `python main.py` (no args, requires config.yaml)
Expected: attempts run (will fail with config missing in CI — that's OK)

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_cli.py
git commit -m "feat: add CLI entry with run subcommand and summary output"
```

---

### Task 11: README & Operational Docs

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write README**

Include sections:
1. 功能简介（多账号、手动/cron、不回溯）
2. 快速开始：`cp config.yaml.example config.yaml`, venv, export passwords, `python main.py run`
3. cron 示例（多 `password_env`）
4. `--account` 调试
5. 目录结构说明
6. 故障排查（AUTH 失败、exit code 非零）

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add README with setup and cron instructions"
```

---

### Task 12: Final Verification

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: all tests PASS, 0 failures

- [ ] **Step 2: Manual smoke checklist (requires real mailbox — optional)**

1. `cp config.yaml.example config.yaml` and edit one account
2. `export IMAP_PASSWORD_HR=...`
3. `python main.py run --account hr-boss`
4. Confirm: first run sets watermark, downloads 0; after sending test mail, second run downloads PDF

- [ ] **Step 3: Commit if any fixups**

---

## Spec Coverage Self-Review

| PRD requirement | Task |
|-----------------|------|
| Multi-account `accounts[]` | Task 7, 9 |
| Per-account download.path | Task 5, 7, 9 |
| Per-account password_env | Task 7 |
| Subject regex (global) | Task 3, 9 |
| Extension whitelist | Task 4, 9 |
| `{date}_{original}` naming | Task 5 |
| Collision `_2`, `_3` | Task 5 |
| `(account_name, message_id)` dedup | Task 6, 9 |
| UID watermark, no backfill | Task 6, 8, 9 |
| Manual `run` + default no subcommand | Task 10 |
| `--account` filter | Task 9, 10 |
| Account failure continues others | Task 9 test + orchestrator |
| Exit non-zero on any failure | Task 10 |
| Log file + stdout summary | Task 10 |
| Mark non-match / no-attachment processed | Task 9 |
| macOS filename sanitize | Task 5 |
| INBOX only readonly | Task 8 |

**Gaps:** None identified. Live IMAP integration test intentionally manual (Out of Scope for CI).

**Placeholder scan:** No TBD/TODO placeholders in task steps.

**Type consistency:** `FetcherFactory`, `MailFetcher`, `ProcessedMailStore` signatures aligned across Tasks 8–10.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-29-mail-resume-fetcher.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration

2. **Inline Execution** — implement tasks in this session with checkpoints after Tasks 3, 6, 9, 12

**Which approach?**

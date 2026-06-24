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
    password_env: str | None


def _resolve_account_password(name: str, imap_raw: dict) -> tuple[str, str | None]:
    inline = imap_raw.get("password")
    if inline is not None and str(inline).strip():
        return str(inline), imap_raw.get("password_env")

    password_env = imap_raw.get("password_env")
    if password_env:
        password = os.environ.get(password_env)
        if not password:
            raise ConfigError(
                f"account {name}: environment variable {password_env} is not set"
            )
        return password, password_env

    raise ConfigError(f"account {name}: imap.password or imap.password_env required")


@dataclass(frozen=True)
class AccountConfig:
    name: str
    imap: ImapSettings
    mailbox: str
    download_path: Path
    password: str
    job_display_id: str = ""
    tenant_id: str = ""
    tenant_code: str = ""


def read_account_job_display_id(item: dict) -> str:
    """读取账号岗位编号，兼容历史配置字段 job_id。"""
    return str(item.get("job_display_id") or item.get("job_id") or "").strip()


def write_account_job_display_id(acct: dict, value: str) -> None:
    """写入账号岗位编号，统一使用 job_display_id 字段。"""
    acct["job_display_id"] = str(value or "").strip()
    acct.pop("job_id", None)


@dataclass(frozen=True)
class PushConfig:
    enabled: bool
    api_url: str
    bearer_token: str
    timeout: int
    success_status_codes: tuple[int, ...]
    host_header: str = ""
    verify_ssl: bool = True


MAX_PARSER_CONCURRENCY = 2


def clamp_parser_concurrency(value: int | str | None, *, default: int = 1) -> int:
    """解析并发上限 2，至少 1。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_PARSER_CONCURRENCY))


@dataclass(frozen=True)
class ParserConfig:
    input_path: Path
    output_path: Path
    archive_path: Path
    recursive: bool
    ark_api_key: str
    ark_base_url: str
    model: str
    concurrency: int
    request_timeout: int
    max_retries: int
    file_attempts: int
    text_truncate: int
    chunk_size: int


@dataclass(frozen=True)
class AppConfig:
    accounts: tuple[AccountConfig, ...]
    subject_patterns: tuple[str, ...]
    subject_exclude_patterns: tuple[str, ...]
    attachment_extensions: tuple[str, ...]
    db_path: Path
    log_path: Path
    parser: ParserConfig | None = None
    push: PushConfig | None = None


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
        password, password_env = _resolve_account_password(name, imap_raw)

        download_raw = item.get("download") or {}
        download_path = download_raw.get("path")
        if not download_path:
            raise ConfigError(f"account {name}: download.path required")

        for key in ("host", "username"):
            if not imap_raw.get(key):
                raise ConfigError(f"account {name}: imap.{key} required")

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
                job_display_id=read_account_job_display_id(item),
                tenant_id=str(item.get("tenant_id") or "").strip(),
                tenant_code=str(item.get("tenant_code") or "").strip(),
            )
        )

    patterns = tuple(raw.get("subject_patterns") or [])
    if not patterns:
        raise ConfigError("subject_patterns must be non-empty")

    exclude_patterns = tuple(raw.get("subject_exclude_patterns") or [])

    extensions = tuple(raw.get("attachment_extensions") or [])
    if not extensions:
        raise ConfigError("attachment_extensions must be non-empty")

    state = raw.get("state") or {}
    log = raw.get("log") or {}
    config_dir = path.parent.resolve()

    db_path = Path(state.get("db_path", "./data/processed.db"))
    log_path = Path(log.get("path", "./logs/resume-fetch.log"))
    if not db_path.is_absolute():
        db_path = config_dir / db_path
    if not log_path.is_absolute():
        log_path = config_dir / log_path

    parser_cfg = _load_parser_config(raw.get("parser") or {}, config_dir)
    push_cfg = _load_push_config(raw.get("push") or {})

    return AppConfig(
        accounts=tuple(accounts),
        subject_patterns=patterns,
        subject_exclude_patterns=exclude_patterns,
        attachment_extensions=extensions,
        db_path=db_path,
        log_path=log_path,
        parser=parser_cfg,
        push=push_cfg,
    )


def _load_parser_config(parser_raw: dict, config_dir: Path) -> ParserConfig | None:
    if not parser_raw:
        return None
    input_path = parser_raw.get("input_path")
    output_path = parser_raw.get("output_path")
    if not input_path or not output_path:
        return None
    api_key = parser_raw.get("ark_api_key") or os.environ.get("ARK_API_KEY") or ""
    archive_path = parser_raw.get("archive_path") or "/Users/admin/Desktop/resume-parsed"
    return ParserConfig(
        input_path=Path(input_path) if Path(input_path).is_absolute() else config_dir / input_path,
        output_path=Path(output_path) if Path(output_path).is_absolute() else config_dir / output_path,
        archive_path=Path(archive_path) if Path(archive_path).is_absolute() else config_dir / archive_path,
        recursive=bool(parser_raw.get("recursive", False)),
        ark_api_key=api_key,
        ark_base_url=str(parser_raw.get("ark_base_url") or "https://ark.cn-beijing.volces.com/api/v3"),
        model=str(parser_raw.get("model") or ""),
        concurrency=clamp_parser_concurrency(parser_raw.get("concurrency"), default=5),
        request_timeout=int(parser_raw.get("request_timeout", 60)),
        max_retries=int(parser_raw.get("max_retries", 1)),
        file_attempts=int(parser_raw.get("file_attempts", 2)),
        text_truncate=int(parser_raw.get("text_truncate", 6000)),
        chunk_size=max(1, int(parser_raw.get("chunk_size", 10))),
    )


def _load_push_config(push_raw: dict) -> PushConfig | None:
    if not push_raw:
        return None
    api_url = str(push_raw.get("api_url") or "").strip()
    if not api_url:
        return PushConfig(
            enabled=False,
            api_url="",
            bearer_token="",
            timeout=int(push_raw.get("timeout", 60)),
            success_status_codes=(200, 201, 204),
            host_header=str(push_raw.get("host_header") or "").strip(),
            verify_ssl=bool(push_raw.get("verify_ssl", True)),
        )
    codes_raw = push_raw.get("success_status_codes") or [200, 201, 204]
    codes = tuple(int(c) for c in codes_raw)
    return PushConfig(
        enabled=bool(push_raw.get("enabled", False)),
        api_url=api_url,
        bearer_token=str(push_raw.get("bearer_token") or "").strip(),
        timeout=int(push_raw.get("timeout", 60)),
        success_status_codes=codes,
        host_header=str(push_raw.get("host_header") or "").strip(),
        verify_ssl=bool(push_raw.get("verify_ssl", True)),
    )

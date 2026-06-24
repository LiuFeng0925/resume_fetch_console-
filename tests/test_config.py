from pathlib import Path

import pytest

from src.config import ConfigError, load_config


@pytest.fixture
def minimal_config_path():
    return Path(__file__).parent / "fixtures" / "minimal_config.yaml"


def test_load_config_parses_accounts(minimal_config_path, monkeypatch):
    monkeypatch.setenv("IMAP_PASSWORD_R2", "secret2")
    cfg = load_config(minimal_config_path)
    assert len(cfg.accounts) == 2
    assert cfg.accounts[0].name == "hr-boss"
    assert cfg.accounts[0].password == "secret1"
    assert cfg.accounts[1].password == "secret2"


def test_inline_password_without_env(tmp_path):
    cfg_path = tmp_path / "solo.yaml"
    cfg_path.write_text(
        """
accounts:
  - name: solo
    imap: {host: h, port: 993, ssl: true, username: a@x.com, password: inline-secret}
    download: {path: /tmp/a}
subject_patterns: [".*"]
attachment_extensions: [".pdf"]
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.accounts[0].password == "inline-secret"
    assert cfg.accounts[0].imap.password_env is None


def test_missing_password_raises(minimal_config_path, monkeypatch):
    monkeypatch.delenv("IMAP_PASSWORD_R2", raising=False)
    with pytest.raises(ConfigError, match="IMAP_PASSWORD_R2"):
        load_config(minimal_config_path)


def test_neither_password_nor_env_raises(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
accounts:
  - name: solo
    imap: {host: h, port: 993, ssl: true, username: a@x.com}
    download: {path: /tmp/a}
subject_patterns: [".*"]
attachment_extensions: [".pdf"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="imap.password or imap.password_env"):
        load_config(bad)


def test_duplicate_account_names_raise(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
accounts:
  - name: dup
    imap: {host: h, port: 993, ssl: true, username: a, password: x}
    download: {path: /tmp/a}
  - name: dup
    imap: {host: h, port: 993, ssl: true, username: b, password: y}
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

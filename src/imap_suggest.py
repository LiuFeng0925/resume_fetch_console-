"""根据邮箱地址推断 IMAP 服务器配置。"""
from __future__ import annotations

from typing import Any

# 常见邮箱服务商 IMAP 配置
KNOWN_IMAP: dict[str, dict[str, Any]] = {
    "163.com": {"host": "imap.163.com", "port": 993, "ssl": True},
    "126.com": {"host": "imap.126.com", "port": 993, "ssl": True},
    "yeah.net": {"host": "imap.yeah.net", "port": 993, "ssl": True},
    "qq.com": {"host": "imap.qq.com", "port": 993, "ssl": True},
    "foxmail.com": {"host": "imap.qq.com", "port": 993, "ssl": True},
    "gmail.com": {"host": "imap.gmail.com", "port": 993, "ssl": True},
    "googlemail.com": {"host": "imap.gmail.com", "port": 993, "ssl": True},
    "outlook.com": {"host": "outlook.office365.com", "port": 993, "ssl": True},
    "hotmail.com": {"host": "outlook.office365.com", "port": 993, "ssl": True},
    "live.com": {"host": "outlook.office365.com", "port": 993, "ssl": True},
    "sina.com": {"host": "imap.sina.com", "port": 993, "ssl": True},
    "sina.cn": {"host": "imap.sina.cn", "port": 993, "ssl": True},
    "sohu.com": {"host": "imap.sohu.com", "port": 993, "ssl": True},
    "139.com": {"host": "imap.139.com", "port": 993, "ssl": True},
    "189.cn": {"host": "imap.189.cn", "port": 993, "ssl": True},
    "aliyun.com": {"host": "imap.aliyun.com", "port": 993, "ssl": True},
}


def _email_domain(email: str) -> str | None:
    email = email.strip().lower()
    if "@" not in email:
        return None
    local, domain = email.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        return None
    return domain


def suggest_imap(
    email: str,
    existing_accounts: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """分层推断 IMAP：常见服务商 → 同域已有账号 → imap.域名 猜测。"""
    domain = _email_domain(email)
    if not domain:
        return None

    if domain in KNOWN_IMAP:
        cfg = KNOWN_IMAP[domain]
        return {
            **cfg,
            "source": "known",
            "message": "已根据常见邮箱服务商自动填写",
        }

    for acct in existing_accounts or []:
        username = str(acct.get("username") or "").strip().lower()
        if "@" not in username:
            continue
        acct_domain = username.rsplit("@", 1)[1]
        if acct_domain != domain:
            continue
        host = str(acct.get("imap_host") or acct.get("host") or "").strip()
        if not host:
            imap = acct.get("imap") or {}
            host = str(imap.get("host") or "").strip()
        if host:
            port = acct.get("imap_port") or acct.get("port")
            if port is None:
                imap = acct.get("imap") or {}
                port = imap.get("port", 993)
            ssl = acct.get("imap_ssl")
            if ssl is None:
                imap = acct.get("imap") or {}
                ssl = imap.get("ssl", True)
            return {
                "host": host,
                "port": int(port or 993),
                "ssl": bool(ssl if ssl is not None else True),
                "source": "domain",
                "message": f"已根据同域账号（@{domain}）自动填写",
            }

    guessed_host = f"imap.{domain}"
    return {
        "host": guessed_host,
        "port": 993,
        "ssl": True,
        "source": "guess",
        "message": f"已猜测为 {guessed_host}，请确认是否正确",
    }

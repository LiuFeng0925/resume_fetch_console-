from __future__ import annotations

import copy
import json
import logging
import ssl
from dataclasses import dataclass
from urllib import error, request

from src.config import PushConfig

logger = logging.getLogger(__name__)


@dataclass
class PushHttpResult:
    ok: bool
    status_code: int | None
    body: str
    error_message: str | None = None


def _is_json_success(payload) -> bool:
    if not isinstance(payload, dict):
        return True
    if payload.get("success") is True:
        return True
    if payload.get("ok") is True:
        return True
    code = payload.get("code")
    if code in (0, "0", 200, "200"):
        return True
    status = str(payload.get("status") or "").lower()
    if status in ("success", "ok", "succeeded"):
        return True
    if code is not None and code not in (0, "0", 200, "200"):
        return False
    if payload.get("success") is False or payload.get("ok") is False:
        return False
    err = payload.get("error") or payload.get("message")
    if err and code not in (None, 0, "0", 200, "200"):
        return False
    return True


def _normalize_bearer_token(token: str) -> str:
    t = (token or "").strip()
    if t.lower().startswith("bearer "):
        return t[7:].strip()
    return t


def _strip_push_meta(payload: dict) -> dict:
    cleaned = copy.deepcopy(payload)
    cleaned.pop("_candidate_meta", None)
    return cleaned


def post_candidates(cfg: PushConfig, payload: dict) -> PushHttpResult:
    body = json.dumps(_strip_push_meta(payload), ensure_ascii=False).encode("utf-8")
    token = _normalize_bearer_token(cfg.bearer_token)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if cfg.host_header:
        headers["Host"] = cfg.host_header
    req = request.Request(cfg.api_url, data=body, headers=headers, method="POST")
    ssl_ctx = None
    if cfg.api_url.lower().startswith("https://") and not cfg.verify_ssl:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        with request.urlopen(req, timeout=cfg.timeout, context=ssl_ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(resp.status)
            ok = status in cfg.success_status_codes
            if ok and raw.strip():
                try:
                    ok = _is_json_success(json.loads(raw))
                except json.JSONDecodeError:
                    pass
            if not ok:
                return PushHttpResult(
                    ok=False,
                    status_code=status,
                    body=raw,
                    error_message=_extract_error(raw) or f"接口返回失败 HTTP {status}",
                )
            return PushHttpResult(ok=True, status_code=status, body=raw)
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return PushHttpResult(
            ok=False,
            status_code=exc.code,
            body=raw,
            error_message=_extract_error(raw) or f"HTTP {exc.code}",
        )
    except error.URLError as exc:
        logger.warning("push request failed: %s", exc)
        return PushHttpResult(
            ok=False,
            status_code=None,
            body="",
            error_message=f"网络错误: {exc.reason}",
        )
    except Exception as exc:
        logger.exception("push request failed")
        return PushHttpResult(
            ok=False,
            status_code=None,
            body="",
            error_message=str(exc),
        )


def _extract_error(raw: str) -> str | None:
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:500]
    if isinstance(data, dict):
        for key in ("message", "error", "detail", "msg"):
            val = data.get(key)
            if val:
                return str(val)[:500]
    return raw[:500]

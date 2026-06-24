"""解析失败记录：找回附件、清除旧记录、重新进入解析队列。"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from src.parse_store import ParseRecordStore

logger = logging.getLogger(__name__)


def is_parse_failed(status: str | None) -> bool:
    return bool(status) and not str(status).startswith("成功")


def _find_source_file(
    source_file: str,
    *,
    input_dir: Path,
    archive_dir: Path,
    file_path: str | None,
) -> Path | None:
    name = Path(source_file).name
    in_input = input_dir / name
    if in_input.is_file():
        return in_input

    if file_path:
        p = Path(file_path)
        if p.is_file():
            return p

    in_archive = archive_dir / name
    if in_archive.is_file():
        return in_archive

    stem = Path(name).stem
    ext = Path(name).suffix
    if archive_dir.is_dir():
        for candidate in archive_dir.glob(f"{stem}(*){ext}"):
            if candidate.is_file():
                return candidate
    return None


def prepare_failed_reparses(
    store: ParseRecordStore,
    record_ids: list[int],
    *,
    input_dir: Path,
    archive_dir: Path,
) -> dict:
    """删除失败记录并将附件复制回 input，供下一轮 parse 重新处理并推送。"""
    input_dir.mkdir(parents=True, exist_ok=True)
    prepared: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []

    for rid in record_ids:
        rec = store.get_record(int(rid))
        if not rec:
            errors.append({"id": rid, "error": "记录不存在"})
            continue
        if not is_parse_failed(rec.get("status")):
            skipped.append({
                "id": rid,
                "source_file": rec.get("source_file") or "",
                "reason": "仅解析失败的记录可重试",
            })
            continue

        source_file = str(rec.get("source_file") or "").strip()
        if not source_file:
            errors.append({"id": rid, "error": "缺少文件名"})
            continue

        src = _find_source_file(
            source_file,
            input_dir=input_dir,
            archive_dir=archive_dir,
            file_path=str(rec.get("file_path") or ""),
        )
        if not src:
            errors.append({"id": rid, "source_file": source_file, "error": "未找到简历附件"})
            continue

        dest = input_dir / src.name
        try:
            if src.resolve() != dest.resolve():
                if dest.exists():
                    dest.unlink()
                shutil.copy2(src, dest)
        except OSError as exc:
            errors.append({"id": rid, "source_file": source_file, "error": f"复制文件失败: {exc}"})
            continue

        if not store.delete_record(int(rid)):
            errors.append({"id": rid, "source_file": source_file, "error": "删除旧记录失败"})
            continue

        prepared.append({"id": rid, "source_file": source_file, "input_path": str(dest)})
        logger.info("reparse prepared: id=%s file=%s", rid, source_file)

    return {
        "ok": len(errors) == 0 or len(prepared) > 0,
        "prepared": prepared,
        "prepared_count": len(prepared),
        "skipped": skipped,
        "errors": errors,
    }

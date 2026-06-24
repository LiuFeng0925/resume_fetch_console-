"""数据 → 文件：带时间戳 Excel + 每份简历的 JSON 留档。"""
import json
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Alignment
from . import schema


def write_excel(rows: list[list], out_dir, timestamp: str) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "简历"
    ws.append(schema.excel_headers())
    for row in rows:
        ws.append(row)
    no_wrap = Alignment(wrap_text=False, vertical="top")
    for row_cells in ws.iter_rows(min_row=2):
        for cell in row_cells:
            cell.alignment = no_wrap
    out_path = out_dir / f"解析结果_{timestamp}.xlsx"
    wb.save(out_path)
    return out_path


def dump_json(record: dict, out_dir, source_file: str) -> Path:
    json_dir = Path(out_dir) / "json"
    json_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(source_file).stem
    target = json_dir / f"{stem}.json"
    i = 2
    while target.exists():  # 重名冲突 → 加序号
        target = json_dir / f"{stem}({i}).json"
        i += 1
    payload = dict(record)
    payload["_source_file"] = source_file
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def dump_text(text: str, out_dir, source_file: str) -> Path:
    """仅取文模式：把纯文本写到 out_dir/文本/<stem>.txt，重名加序号。"""
    text_dir = Path(out_dir) / "文本"
    text_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(source_file).stem
    target = text_dir / f"{stem}.txt"
    i = 2
    while target.exists():
        target = text_dir / f"{stem}({i}).txt"
        i += 1
    target.write_text(text, encoding="utf-8")
    return target

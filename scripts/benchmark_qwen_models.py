#!/usr/bin/env python3
"""对比 Qwen 模型：准确率、速度、成本（90s 超时）。"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from resume_parser.extractor import extract_text
from resume_parser.llm_client import build_messages, parse_response, make_client, _call_chat_completion
from src.config import load_config

HARD_TIMEOUT = 90.0

# OpenRouter 定价 USD / 1M tokens（2025-06，来自 openrouter.ai 页面）
PRICING = {
    "qwen/qwen3-235b-a22b-2507": (0.071, 0.10),
    "qwen/qwen-plus": (0.40, 1.20),
    "qwen/qwen-2.5-72b-instruct": (0.07, 0.26),
    "qwen/qwen3.6-flash": (0.10, 0.40),
    "qwen/qwen3-30b-a3b-instruct-2507": (0.08, 0.28),
    "qwen/qwen-turbo": (0.05, 0.20),
    "qwen/qwen3-max": (1.20, 6.00),
    "qwen/qwen-2.5-7b-instruct": (0.04, 0.10),
    "qwen/qwen3.5-flash-02-23": (0.10, 0.40),
    "qwen/qwen3.5-plus-02-15": (0.40, 2.40),
}

MODELS = [
    ("qwen/qwen3-235b-a22b-2507", "Qwen3 235B MoE 2507（当前）"),
    ("qwen/qwen-plus", "Qwen Plus 通用"),
    ("qwen/qwen-2.5-72b-instruct", "Qwen2.5 72B"),
    ("qwen/qwen3.6-flash", "Qwen3.6 Flash"),
    ("qwen/qwen3-30b-a3b-instruct-2507", "Qwen3 30B MoE"),
    ("qwen/qwen-turbo", "Qwen Turbo 便宜"),
    ("qwen/qwen-2.5-7b-instruct", "Qwen2.5 7B 最便宜"),
]


def _name_from_filename(path: Path) -> str:
    stem = path.stem
    if "】" in stem:
        tail = stem.split("】", 1)[1]
        parts = tail.split("_")
        if parts:
            return parts[0]
    return ""


def _accuracy_score(expected: str, parsed: dict) -> float:
    if not parsed:
        return 0.0
    score = 0.0
    name = str(parsed.get("name") or "").strip()
    phone = str(parsed.get("phone") or "").strip()
    if name:
        score += 0.4
        if expected and (expected in name or name in expected):
            score += 0.3
    if phone and len(phone) >= 11:
        score += 0.2
    if parsed.get("work_experiences") or parsed.get("education_history"):
        score += 0.1
    return min(score, 1.0)


def _est_cost(model: str, inp_chars: int, out_chars: int) -> float:
    pin, pout = PRICING.get(model, (0.1, 0.3))
    # 粗估：中文约 1.5 char/token，英文约 4 char/token，取 2
    in_tok = inp_chars / 2
    out_tok = out_chars / 2
    return (in_tok * pin + out_tok * pout) / 1_000_000


def load_samples(limit: int = 5) -> list[dict]:
    paths = [
        Path("/Users/admin/Desktop/resume-parsed/20260615_171227_0001_【HR专员保底3500_泰安_4-7K】李一晨_3年_13145389666@163.com.pdf"),
    ]
    for p in sorted(Path("/Users/admin/Desktop/resume-parsed").glob("*.pdf")):
        if p not in paths:
            paths.append(p)
        if len(paths) >= limit:
            break
    samples = []
    for p in paths[:limit]:
        text, ocr = extract_text(p)
        samples.append({
            "path": p,
            "label": _name_from_filename(p) or p.stem[:20],
            "expected_name": _name_from_filename(p),
            "text": text,
            "len": len(text),
            "ocr": ocr,
        })
    return samples


def run_benchmark():
    cfg = load_config(ROOT / "config.yaml")
    client = make_client(cfg.parser)
    samples = load_samples(5)

    print(f"超时: {HARD_TIMEOUT}s | 样本数: {len(samples)}")
    print("样本:", ", ".join(f"{s['label']}({s['len']}字)" for s in samples))
    print()

    all_results = []
    for model, label in MODELS:
        ok = fail = timeout = 0
        times: list[float] = []
        acc_scores: list[float] = []
        costs: list[float] = []
        details: list[str] = []

        for si, sample in enumerate(samples):
            messages = build_messages(sample["text"])
            inp_len = sum(len(m["content"]) for m in messages)
            t0 = time.time()
            content = ""
            try:
                resp = _call_chat_completion(
                    client, model=model, messages=messages, hard_timeout=HARD_TIMEOUT,
                )
                content = (resp.choices[0].message.content or "").strip()
                elapsed = time.time() - t0
                data = parse_response(content)
                acc = _accuracy_score(sample["expected_name"], data)
                cost = _est_cost(model, inp_len, len(content))
                ok += 1
                times.append(elapsed)
                acc_scores.append(acc)
                costs.append(cost)
                details.append(
                    f"  {sample['label']}: OK {elapsed:.1f}s acc={acc:.0%} "
                    f"name={data.get('name')!r}"
                )
            except TimeoutError:
                timeout += 1
                fail += 1
                details.append(f"  {sample['label']}: TIMEOUT >{HARD_TIMEOUT:.0f}s")
            except Exception as e:
                fail += 1
                elapsed = time.time() - t0
                raw = content[:40] if content else ""
                details.append(f"  {sample['label']}: FAIL {elapsed:.1f}s raw={raw!r} {e}")

        total = ok + fail
        row = {
            "model": model,
            "label": label,
            "ok": ok,
            "total": total,
            "timeout": timeout,
            "success_rate": ok / total if total else 0,
            "avg_time": sum(times) / len(times) if times else None,
            "avg_acc": sum(acc_scores) / len(acc_scores) if acc_scores else 0,
            "total_cost": sum(costs),
            "cost_per_ok": sum(costs) / ok if ok else None,
            "details": details,
        }
        all_results.append(row)

        avg_t = f"{row['avg_time']:.1f}s" if row["avg_time"] else "—"
        print(f"【{label}】 {model}")
        print(f"  成功 {ok}/{total} | 超时 {timeout} | 均速 {avg_t} | 准确率 {row['avg_acc']:.0%} | 估成本 ${row['total_cost']:.5f}")
        for d in details:
            print(d)
        print()

    # 综合评分：成功率 50% + 准确率 30% + 速度 10% + 成本 10%
    def score(r):
        if r["success_rate"] == 0:
            return -1
        speed = 1.0 / (1.0 + (r["avg_time"] or 90) / 30.0)
        cost = 1.0 / (1.0 + (r["cost_per_ok"] or 0.01) * 10000)
        return r["success_rate"] * 0.5 + r["avg_acc"] * 0.3 + speed * 0.1 + cost * 0.1

    ranked = sorted(all_results, key=score, reverse=True)
    print("=" * 60)
    print("综合排名（成功率50% + 准确率30% + 速度10% + 成本10%）")
    print("=" * 60)
    for i, r in enumerate(ranked, 1):
        avg_t = f"{r['avg_time']:.1f}s" if r["avg_time"] else "超时"
        cpo = f"${r['cost_per_ok']:.6f}" if r["cost_per_ok"] else "—"
        print(
            f"{i}. {r['label']}\n"
            f"   slug: {r['model']}\n"
            f"   成功 {r['ok']}/{r['total']} ({r['success_rate']:.0%}) | "
            f"准确率 {r['avg_acc']:.0%} | 均速 {avg_t} | 单次成本 {cpo}"
        )

    best = ranked[0]
    print()
    print("推荐:", best["model"])
    print("理由: 在 90s 超时约束下综合得分最高。")

    out = ROOT / "data" / "qwen_benchmark_result.json"
    out.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n详细结果已保存: {out}")


if __name__ == "__main__":
    run_benchmark()

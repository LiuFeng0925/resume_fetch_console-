#!/bin/bash
# 定时抓取入口（launchd / 手动均可调用）
ROOT="/Users/admin/Desktop/邮箱抓取简历"
cd "$ROOT" || exit 1
mkdir -p "$ROOT/logs"
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') scheduled run ====="
  /usr/local/bin/python3 "$ROOT/main.py" run
  echo "===== exit $? ====="
} >> "$ROOT/logs/cron.log" 2>&1

#!/usr/bin/env python3
"""
白夜系统 — 纸交易后台循环
每15分钟调用一次 paper_trading.py，持续运行
"""
import time, subprocess, sys
from datetime import datetime, timezone

INTERVAL = 15 * 60  # 15分钟

def log(msg):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    print(f"[{ts}] {msg}", flush=True)

log("🚀 纸交易后台循环启动")
log(f"扫描间隔: {INTERVAL//60}分钟")

cycle = 0
while True:
    cycle += 1
    log(f"=== 第{cycle}轮扫描 ===")
    try:
        result = subprocess.run(
            [sys.executable, 'paper_trading.py'],
            capture_output=True, text=True, timeout=120,
            cwd='/root/.openclaw/workspace/killer-trading-system'
        )
        if result.stdout:
            print(result.stdout, flush=True)
        if result.returncode != 0 and result.stderr:
            log(f"⚠️ stderr: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        log("⚠️ 扫描超时（>120s）")
    except Exception as e:
        log(f"❌ 异常: {e}")

    log(f"下次扫描: {INTERVAL//60}分钟后")
    time.sleep(INTERVAL)

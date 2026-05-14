#!/bin/bash
# watchdog_v73.sh — v7.3 引擎铁桶级守护脚本
# 功能：检查引擎是否运行，死了就重启，完全脱离网关会话
# 被 cron 调用，每次执行只做一件事：确保引擎活着

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/data/baiye_v73.pid"
LOG_FILE="$SCRIPT_DIR/logs/baiye_v73.log"
STDOUT_LOG="$SCRIPT_DIR/logs/baiye_v73_stdout.log"
ENGINE="$SCRIPT_DIR/main_v73.py"
WATCHDOG_LOG="$SCRIPT_DIR/logs/watchdog_v73.log"

mkdir -p "$SCRIPT_DIR/data" "$SCRIPT_DIR/logs"

ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

log() { echo "[$(ts)] $*" | tee -a "$WATCHDOG_LOG"; }

# ── 检查引擎是否存活 ──
is_alive() {
    if [ ! -f "$PID_FILE" ]; then return 1; fi
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null) || return 1
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# ── 启动引擎（完全脱离会话，免疫 SIGHUP/SIGTERM 传播）──
start_engine() {
    log "🚀 启动 v7.3 引擎..."
    # setsid 创建新会话；nohup 忽略 SIGHUP；>> 追加日志
    nohup setsid python3 "$ENGINE" \
        >> "$STDOUT_LOG" 2>&1 &
    local pid=$!
    disown $pid 2>/dev/null || true
    sleep 2
    if kill -0 "$pid" 2>/dev/null; then
        log "✅ 引擎启动成功 PID=$pid"
        # 写入 pid 文件（引擎本身也会写，这里做双保险）
        echo "$pid" > "$PID_FILE"
        return 0
    else
        log "❌ 引擎启动失败，查看 $STDOUT_LOG"
        return 1
    fi
}

# ── 主逻辑 ──
if is_alive; then
    pid=$(cat "$PID_FILE")
    log "✅ 引擎运行中 PID=$pid，无需操作"
    exit 0
else
    log "⚠️  引擎未运行（PID文件: $([ -f "$PID_FILE" ] && cat "$PID_FILE" || echo '不存在')），准备重启..."
    rm -f "$PID_FILE"
    start_engine
fi

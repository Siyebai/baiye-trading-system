#!/bin/bash
# ══════════════════════════════════════════════════════════
# 白夜交易系统 v7.3 — Guardian守护脚本
# 功能: 引擎崩溃后自动重启，保证24/7稳定运行
# 用法: nohup bash guardian_scripts/start_baiye_v73.sh &
# ══════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
ENGINE="main_v73.py"
LOG_DIR="$BASE_DIR/logs"
STDOUT_LOG="$LOG_DIR/baiye_v73_stdout.log"
GUARDIAN_LOG="$LOG_DIR/guardian_v73.log"
PID_FILE="$BASE_DIR/data/baiye_v73.pid"
MAX_RESTARTS=999
RESTART_DELAY=10   # 重启前等待秒数
MEMORY_LIMIT_MB=2800  # 内存超限触发重启

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$GUARDIAN_LOG"
}

check_memory() {
    local pid=$1
    if [ -z "$pid" ]; then return 0; fi
    local rss
    rss=$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ')
    if [ -n "$rss" ]; then
        local mb=$((rss / 1024))
        if [ "$mb" -gt "$MEMORY_LIMIT_MB" ]; then
            log "⚠️  内存超限 ${mb}MB > ${MEMORY_LIMIT_MB}MB，触发重启"
            return 1
        fi
    fi
    return 0
}

log "═══ 白夜Guardian v7.3 启动 ═══"
log "引擎: $BASE_DIR/$ENGINE"
log "最大重启次数: $MAX_RESTARTS"

restart_count=0

while [ "$restart_count" -lt "$MAX_RESTARTS" ]; do
    # 杀掉旧进程
    if [ -f "$PID_FILE" ]; then
        old_pid=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
            log "停止旧进程 PID=$old_pid"
            kill -15 "$old_pid" 2>/dev/null
            sleep 3
        fi
    fi

    log "启动引擎 (第$((restart_count+1))次) ..."
    cd "$BASE_DIR" || exit 1
    python3 -u "$ENGINE" >> "$STDOUT_LOG" 2>&1 &
    ENGINE_PID=$!
    log "引擎PID=$ENGINE_PID"
    echo "$ENGINE_PID" > "$PID_FILE"

    # 监控循环
    while kill -0 "$ENGINE_PID" 2>/dev/null; do
        sleep 30
        if ! check_memory "$ENGINE_PID"; then
            kill -15 "$ENGINE_PID" 2>/dev/null
            sleep 5
            break
        fi
    done

    EXIT_CODE=$?
    restart_count=$((restart_count + 1))
    log "❌ 引擎退出 (exit=$EXIT_CODE, 已重启=$restart_count次)，${RESTART_DELAY}秒后重启..."
    sleep "$RESTART_DELAY"
done

log "达到最大重启次数 $MAX_RESTARTS，Guardian退出"

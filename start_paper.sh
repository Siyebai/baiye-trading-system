#!/bin/bash
# 杀手锏 纸交易引擎 安全启动脚本 v1.0.6
# 功能：检查单例、清理旧实例、后台启动、记录启动日志

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
PID_FILE="$LOG_DIR/paper_engine.pid"
LOG_FILE="$LOG_DIR/paper_engine.log"
ENGINE="$SCRIPT_DIR/scripts/paper_engine_v106.py"

mkdir -p "$LOG_DIR"

echo "⚔️  杀手锏 纸交易启动脚本"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── 检查是否已有实例 ──
if [ -f "$PID_FILE" ]; then
    EXISTING_PID=$(cat "$PID_FILE" 2>/dev/null)
    if kill -0 "$EXISTING_PID" 2>/dev/null; then
        echo "❌ 已有实例在运行 PID=$EXISTING_PID"
        echo "   查看日志: tail -f $LOG_FILE"
        echo "   停止运行: kill $EXISTING_PID"
        exit 1
    else
        echo "⚠️  发现旧PID文件但进程已死，清理中..."
        rm -f "$PID_FILE"
    fi
fi

# ── 参数 ──
MODE="${1:---72h}"   # 默认 72h 模式；传 --once 则单次扫描

if [ "$MODE" = "--once" ]; then
    echo "▶ 模式: 单次扫描"
    python3 "$ENGINE" --once
else
    echo "▶ 模式: 72小时后台运行"
    echo "▶ 日志: $LOG_FILE"
    nohup python3 "$ENGINE" 2>&1 &
    BGPID=$!
    sleep 1
    if kill -0 "$BGPID" 2>/dev/null; then
        echo "✅ 启动成功  PID=$BGPID"
        echo "   查看日志: tail -f $LOG_FILE"
        echo "   停止运行: kill $BGPID"
    else
        echo "❌ 启动失败，查看日志: $LOG_FILE"
        exit 1
    fi
fi

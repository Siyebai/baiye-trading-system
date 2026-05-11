#!/bin/bash
# ============================================================
# paper_guardian_v70.sh — 白夜交易系统 v7.0 进程守护
# 功能：检查v7.0进程，挂掉自动重启
# ============================================================

LOG_FILE="/root/.openclaw/workspace/logs/paper_guardian.log"
PID_FILE="/root/.openclaw/workspace/killer-trading-system/logs/paper_v70.pid"
WORK_DIR="/root/.openclaw/workspace/killer-trading-system"
SCRIPT="main_v70.py"
PAPER_LOG="logs/paper_v70.log"
MAX_LOG_LINES=500
MAX_MEM_MB=500

mkdir -p "$(dirname "$LOG_FILE")"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [GUARDIAN-v70] $*" >> "$LOG_FILE"; }

# 日志轮转
if [ -f "$LOG_FILE" ]; then
    lines=$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$lines" -gt "$MAX_LOG_LINES" ]; then
        tail -200 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
    fi
fi

# 读取PID
CURRENT_PID=""
if [ -f "$PID_FILE" ]; then
    CURRENT_PID=$(cat "$PID_FILE" 2>/dev/null)
fi

# 检查进程存活
is_alive() {
    local pid=$1
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

if is_alive "$CURRENT_PID"; then
    RSS=$(awk '/VmRSS/ {print $2}' /proc/$CURRENT_PID/status 2>/dev/null || echo 0)
    RSS_MB=$((RSS/1024))

    # 从state文件获取进度
    PROGRESS=$(python3 -c "
import json, os
f='/root/.openclaw/workspace/killer-trading-system/logs/paper_v70_state.json'
if os.path.exists(f):
    d=json.load(open(f))
    wins=d.get('wins',0); losses=d.get('losses',0)
    total=wins+losses
    wr=wins/total*100 if total else 0
    pnl=d.get('total_pnl',0)
    eq=d.get('equity',150)
    dd=d.get('max_drawdown',0)
    print(f'{total}/100笔 WR={wr:.0f}% PnL={pnl:+.2f}U 权益={eq:.2f}U 回撤={dd:.1f}%')
else:
    print('未开始')
" 2>/dev/null || echo "状态未知")

    log "✅ v7.0 运行中 PID=$CURRENT_PID RSS=${RSS_MB}MB | $PROGRESS"

    if [ "$RSS_MB" -gt "$MAX_MEM_MB" ]; then
        log "⚠️ 内存过高${RSS_MB}MB > ${MAX_MEM_MB}MB，优雅重启..."
        kill -15 "$CURRENT_PID" 2>/dev/null
        sleep 5
        kill -9 "$CURRENT_PID" 2>/dev/null 2>&1
    else
        exit 0
    fi
fi

# 重启
log "⚡ v7.0 进程不存在，自动重启..."
cd "$WORK_DIR" || { log "❌ 无法进入工作目录"; exit 1; }

nohup python3 "$SCRIPT" >> "$PAPER_LOG" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
log "✅ v7.0 已重启 PID=$NEW_PID"

#!/bin/bash
# ============================================================
# paper_guardian.sh v2.0 — 白夜纸交易进程守护（v6.5）
# 功能：检查paper v6.5进程，挂掉自动重启
# ============================================================

LOG_FILE="/root/.openclaw/workspace/logs/paper_guardian.log"
PID_FILE="/root/.openclaw/workspace/logs/paper_engine.pid"
WORK_DIR="/root/.openclaw/workspace/killer-trading-system"
SCRIPT="main_paper_trade.py"
PAPER_LOG="logs/paper_v69.log"
MAX_LOG_LINES=300

mkdir -p "$(dirname "$LOG_FILE")"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [PAPER-GUARDIAN] $*" >> "$LOG_FILE"; }

# 日志轮转
if [ -f "$LOG_FILE" ]; then
    lines=$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$lines" -gt "$MAX_LOG_LINES" ]; then
        tail -100 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
    fi
fi

# 读取PID
CURRENT_PID=""
if [ -f "$PID_FILE" ]; then
    CURRENT_PID=$(cat "$PID_FILE" 2>/dev/null)
fi

# 检查进程是否存活
is_alive() {
    local pid=$1
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

if is_alive "$CURRENT_PID"; then
    RSS=$(awk '/VmRSS/ {print $2}' /proc/$CURRENT_PID/status 2>/dev/null || echo 0)
    RSS_MB=$((RSS/1024))
    
    # 获取纸交易进度
    PROGRESS=$(python3 -c "
import json, os
f='/root/.openclaw/workspace/killer-trading-system/paper_trades_v64.json'
if os.path.exists(f):
    d=json.load(open(f))
    closed=[t for t in d if t.get('status')=='closed']
    if closed:
        wins=[t for t in closed if t.get('pnl',0)>0]
        pnl=sum(t.get('pnl',0) for t in closed)
        wr=len(wins)/len(closed)*100
        print(f'{len(closed)}/100笔 WR={wr:.0f}% PnL={pnl:+.2f}U')
    else:
        print('0/100笔')
else:
    print('未开始')
" 2>/dev/null || echo "状态未知")
    
    log "✅ paper v6.5 运行中 PID=$CURRENT_PID RSS=${RSS_MB}MB 进度=$PROGRESS"
    
    if [ "$RSS_MB" -gt 500 ]; then
        log "⚠️ paper进程内存过高${RSS_MB}MB，重启..."
        kill -15 "$CURRENT_PID" 2>/dev/null
        sleep 3
        kill -9 "$CURRENT_PID" 2>/dev/null
    else
        exit 0
    fi
fi

# 重启
log "⚡ paper v6.5 进程不存在，自动重启..."
cd "$WORK_DIR" || { log "❌ 无法进入工作目录"; exit 1; }

nohup python3 "$SCRIPT" >> "$PAPER_LOG" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
log "✅ paper v6.5 已重启 新PID=$NEW_PID"
echo "🔄 白夜纸交易v6.5已自动重启 PID=$NEW_PID"

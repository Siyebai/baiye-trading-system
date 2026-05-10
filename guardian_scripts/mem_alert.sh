#!/bin/bash
# ============================================================
# mem_alert.sh v3.0 — 内存告警推送
# 更新：2026-05-10 深度修复
#   - 告警阈值: 85% → 75%
#   - 冷却时间: 30min → 20min（更及时）
#   - 新增: Gateway内存状态显示
# ============================================================

THRESHOLD=75       # 告警阈值%（原85）
LOCKFILE="/tmp/.mem_alert_lock"
COOLDOWN=1200      # 20分钟冷却（原1800）

MEM_TOTAL=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
MEM_AVAIL=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
MEM_USED=$((MEM_TOTAL - MEM_AVAIL))
MEM_PCT=$((MEM_USED * 100 / MEM_TOTAL))
MEM_AVAIL_MB=$((MEM_AVAIL / 1024))
LOAD=$(awk '{print $1}' /proc/loadavg)

# 正常则退出（无输出=静默=cron不发消息）
if [ "$MEM_PCT" -lt "$THRESHOLD" ]; then
    rm -f "$LOCKFILE"
    exit 0
fi

# 冷却检查
if [ -f "$LOCKFILE" ]; then
    last=$(cat "$LOCKFILE" 2>/dev/null)
    now=$(date +%s)
    diff=$((now - last))
    if [ "$diff" -lt "$COOLDOWN" ]; then
        exit 0
    fi
fi
date +%s > "$LOCKFILE"

# Gateway内存
GW_PID=$(pgrep -f openclaw-gateway 2>/dev/null | head -1)
GW_INFO="未运行"
if [ -n "$GW_PID" ]; then
    GW_RSS=$(awk '/VmRSS/ {print $2}' /proc/$GW_PID/status 2>/dev/null || echo 0)
    GW_MB=$((GW_RSS / 1024))
    GW_UPTIME=$(ps -p $GW_PID -o etimes= 2>/dev/null | tr -d ' ')
    GW_UPTIME_H=$((${GW_UPTIME:-0} / 3600))
    GW_INFO="${GW_MB}MB (运行${GW_UPTIME_H}h)"
fi

# TOP3进程
TOP3=$(ps aux --sort=-%mem | awk 'NR>1 && NR<=4 {
    cmd=$11; sub(/.*\//, "", cmd)
    printf "%s(%.0f%%) ", cmd, $4
}')

# 告警级别
if [ "$MEM_PCT" -ge 88 ]; then
    LEVEL="🔴 紧急"
elif [ "$MEM_PCT" -ge 75 ]; then
    LEVEL="🟡 警告"
fi

echo "${LEVEL} 内存 ${MEM_PCT}% | 可用 ${MEM_AVAIL_MB}MB | 负载 ${LOAD}"
echo "Gateway: ${GW_INFO} | 高耗: ${TOP3}"
echo "守卫已自动清理。如持续告警请重启gateway。"

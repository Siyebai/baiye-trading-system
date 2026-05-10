#!/bin/bash
# ============================================================
# gateway_restart.sh v1.0 — 定期重启Gateway防内存泄漏
# 每12小时执行，判断当前内存>500MB则触发重启
# ============================================================

LOG_FILE="/root/.openclaw/workspace/logs/sys_guardian.log"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [GW-RESTART] $*" >> "$LOG_FILE"; }

# v1.1修复: 通过comm精确匹配，避免误匹配含gateway字样的shell脚本
GW_PID=$()
for _pid in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
    _comm=$(cat /proc/$_pid/comm 2>/dev/null) || continue
    if [ "$_comm" = "openclaw-gatewa" ] || [ "$_comm" = "openclaw-gateway" ]; then
        GW_PID="$_pid"
        break
    fi
done
if [ -z "$GW_PID" ]; then
    log "gateway未运行，跳过"
    exit 0
fi

GW_RSS=$(awk '/VmRSS/ {print $2}' /proc/$GW_PID/status 2>/dev/null || echo 0)
GW_MB=$((GW_RSS / 1024))
GW_UPTIME=$(ps -p $GW_PID -o etimes= 2>/dev/null | tr -d ' ')
GW_UPTIME_H=$((${GW_UPTIME:-0} / 3600))

log "定期检查 PID=$GW_PID RSS=${GW_MB}MB 运行${GW_UPTIME_H}h"

# 运行超过6小时且内存>500MB才重启（避免刚重启就又杀）
MIN_UPTIME_S=21600  # 6小时
MIN_RSS_MB=500

if [ "${GW_UPTIME:-0}" -gt "$MIN_UPTIME_S" ] && [ "$GW_MB" -gt "$MIN_RSS_MB" ]; then
    log "⚡ 触发定期重启: 运行${GW_UPTIME_H}h, 内存${GW_MB}MB > ${MIN_RSS_MB}MB"
    kill -15 "$GW_PID" 2>/dev/null
    echo "🔄 Gateway定期重启 | 运行${GW_UPTIME_H}h | 内存${GW_MB}MB → 释放中"
else
    log "✅ 无需重启: 运行${GW_UPTIME_H}h(需>6h), 内存${GW_MB}MB(需>${MIN_RSS_MB}MB)"
    exit 0
fi

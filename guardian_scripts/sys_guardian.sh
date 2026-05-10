#!/bin/bash
# ============================================================
# sys_guardian.sh v3.1 — 系统保障守卫
# 功能：内存监控 + Gateway泄漏重启 + 分级清理 + 日志
# 执行：cron每5分钟调用
# 更新：2026-05-10 v3.1修复
#   - 修复Bug: pgrep -f误匹配config-watcher脚本（含gateway字样）
#   - 改为通过/proc/pid/comm精确匹配进程名
#   - 告警阈值: 85% → 75%
#   - Gateway内存上限: 900MB → 750MB（超限SIGTERM+等待重启）
#   - 紧急阈值: 92% → 88%
#   - 新增: Gateway连续异常计数（3次未恢复则强制kill -9）
# ============================================================

LOG_FILE="/root/.openclaw/workspace/logs/sys_guardian.log"
COUNTER_FILE="/tmp/.gw_oversize_count"
ALERT_THRESHOLD=75      # 警告阈值%（原85）
CRITICAL_THRESHOLD=88   # 紧急阈值%（原92）
MAX_GATEWAY_MB=750      # gateway允许最大内存MB（原900）
MAX_LOG_LINES=500

mkdir -p "$(dirname "$LOG_FILE")"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [GUARDIAN] $*" >> "$LOG_FILE"; }
logecho() { echo "$(date '+%Y-%m-%d %H:%M:%S') [GUARDIAN] $*" | tee -a "$LOG_FILE"; }

# 日志轮转
if [ -f "$LOG_FILE" ]; then
    lines=$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$lines" -gt "$MAX_LOG_LINES" ]; then
        tail -200 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
        log "日志轮转（保留200行）"
    fi
fi

# ── 读取内存 ──────────────────────────────────────────────
MEM_TOTAL=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
MEM_AVAIL=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
MEM_USED=$((MEM_TOTAL - MEM_AVAIL))
MEM_PCT=$((MEM_USED * 100 / MEM_TOTAL))
MEM_AVAIL_MB=$((MEM_AVAIL / 1024))
LOAD=$(awk '{print $1}' /proc/loadavg)

log "内存: ${MEM_PCT}% | 可用: ${MEM_AVAIL_MB}MB | 负载: $LOAD"

# ── 常规清理（每次必做）──────────────────────────────────
clean_routine() {
    find /tmp -maxdepth 2 -name "*.log" -mtime +0 -delete 2>/dev/null
    find /tmp -maxdepth 2 -name "*.cache" -delete 2>/dev/null
    find /tmp -maxdepth 2 -name "node-compile-cache" -type d -exec rm -rf {} + 2>/dev/null
    find /tmp -maxdepth 2 -name "jiti*" -exec rm -rf {} + 2>/dev/null
    find /root/.openclaw/workspace -name "*.pyc" -delete 2>/dev/null
    find /root/.openclaw/workspace -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
    find /root/.openclaw -path "*/_logs/*.log" -delete 2>/dev/null
    find /root/.cache/pip -type f -delete 2>/dev/null
}

# ── 检查Gateway内存泄漏（升级版）────────────────────────
# v3.1修复：通过comm=openclaw-gatewa精确匹配，避免误匹配含gateway字样的shell脚本
find_gateway_pid() {
    for pid in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
        comm=$(cat /proc/$pid/comm 2>/dev/null) || continue
        if [ "$comm" = "openclaw-gatewa" ] || [ "$comm" = "openclaw-gateway" ]; then
            echo "$pid"
            return 0
        fi
    done
    # 备用：通过exe路径匹配node进程+comm
    return 1
}

check_gateway() {
    GW_PID=$(find_gateway_pid)
    if [ -z "$GW_PID" ]; then
        log "gateway未运行"
        rm -f "$COUNTER_FILE"
        return
    fi

    GW_RSS=$(awk '/VmRSS/ {print $2}' /proc/$GW_PID/status 2>/dev/null || echo 0)
    GW_MB=$((GW_RSS / 1024))
    GW_UPTIME=$(ps -p $GW_PID -o etimes= 2>/dev/null | tr -d ' ')
    GW_UPTIME_H=$((${GW_UPTIME:-0} / 3600))
    log "gateway PID=$GW_PID RSS=${GW_MB}MB 已运行${GW_UPTIME_H}h"

    if [ "$GW_MB" -gt "$MAX_GATEWAY_MB" ]; then
        COUNT=1
        if [ -f "$COUNTER_FILE" ]; then
            COUNT=$(($(cat "$COUNTER_FILE" 2>/dev/null || echo 0) + 1))
        fi
        echo "$COUNT" > "$COUNTER_FILE"
        log "⚠️ gateway内存超限: ${GW_MB}MB > ${MAX_GATEWAY_MB}MB (第${COUNT}次)"

        if [ "$COUNT" -ge 3 ]; then
            # 连续3次超限：强制kill -9
            log "🔴 连续${COUNT}次超限，强制kill -9 PID=$GW_PID"
            kill -9 "$GW_PID" 2>/dev/null
            rm -f "$COUNTER_FILE"
            echo "GATEWAY_KILLED:${GW_MB}MB"
        else
            # SIGTERM优雅退出（系统会自动重启）
            log "发送SIGTERM到PID=$GW_PID（等待优雅退出+自动重启）"
            kill -15 "$GW_PID" 2>/dev/null
            echo "GATEWAY_RESTART:${GW_MB}MB"
        fi
    else
        # 正常，重置计数
        rm -f "$COUNTER_FILE"
    fi
}

# ── 预防清理 ─────────────────────────────────────────────
clean_preventive() {
    rm -rf /tmp/node-compile-cache /tmp/jiti /tmp/npm-* 2>/dev/null
    find /root/.openclaw -path "*/_logs/*.log" -delete 2>/dev/null
    find /root/.openclaw/workspace -name "*.log" -size +2M -exec truncate -s 1M {} \; 2>/dev/null
    find /root/.cache -type f -mtime +1 -delete 2>/dev/null
    log "预防清理完成"
}

# ── 紧急清理 ─────────────────────────────────────────────
clean_emergency() {
    log "🔴 紧急清理启动！"
    rm -rf /tmp/node-compile-cache /tmp/jiti /tmp/npm-* /tmp/*.tmp /tmp/openclaw* 2>/dev/null
    find /root/.openclaw -name "*.log" -size +500k -exec truncate -s 0 {} \; 2>/dev/null
    find /var/log -name "*.log" -size +5M -exec truncate -s 0 {} \; 2>/dev/null
    rm -rf /root/.cache/pip 2>/dev/null
    sync 2>/dev/null
    log "TOP内存进程:"
    ps aux --sort=-%mem | awk 'NR>1 && NR<=6 {printf "  PID=%-6s MEM=%-5s CMD=%s\n",$2,$4,$11}' >> "$LOG_FILE"
    MEM_AFTER=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
    log "紧急清理后可用: $((MEM_AFTER/1024))MB"
}

# ── 主逻辑 ───────────────────────────────────────────────
clean_routine
check_gateway

if [ "$MEM_PCT" -ge "$CRITICAL_THRESHOLD" ]; then
    clean_emergency
    logecho "🔴 紧急: 内存${MEM_PCT}% 超过${CRITICAL_THRESHOLD}%阈值！"
    exit 2
elif [ "$MEM_PCT" -ge "$ALERT_THRESHOLD" ]; then
    clean_preventive
    logecho "⚠️ 警告: 内存${MEM_PCT}% 超过${ALERT_THRESHOLD}%阈值"
    exit 1
else
    log "✅ 正常: 内存${MEM_PCT}% 负载${LOAD}"
    exit 0
fi

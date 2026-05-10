# Guardian Scripts — 系统守护脚本

## 文件说明
| 文件 | 功能 | 触发方式 |
|------|------|---------|
| `sys_guardian.sh` | 内存监控+Gateway泄漏重启 | cron每5分钟 |
| `paper_guardian.sh` | 纸交易进程守护（挂掉自动重启） | cron每5分钟 |
| `gateway_restart.sh` | Gateway定期重启防泄漏 | cron每12小时 |
| `mem_alert.sh` | 内存超阈值推送告警 | cron每20分钟 |

## 关键修复（v3.1 2026-05-10）
- **Bug修复**: `pgrep -f openclaw-gateway` 会误匹配config-watcher脚本
- **修复方案**: 改用 `/proc/pid/comm` 精确匹配进程名 `openclaw-gatewa`
- **告警阈值**: 75%/88%（原85%/92%）
- **Gateway上限**: 750MB（原900MB）

## 部署
```bash
# cron jobs（已配置，无需手动操作）
openclaw cron list
```

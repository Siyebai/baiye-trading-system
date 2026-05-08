"""
日志工具模块 — 统一日志配置（含轮转）
所有交易系统模块应使用此模块创建logger，防止日志无限增长
"""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def get_logger(name: str, log_file: Path, max_bytes: int = 5*1024*1024, backup_count: int = 3) -> logging.Logger:
    """
    创建带轮转的logger
    - max_bytes: 单文件最大5MB
    - backup_count: 保留3个备份
    - 总计最大: 5MB × 4 = 20MB
    """
    log_file.parent.mkdir(exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # 防止重复添加handler
    
    logger.setLevel(logging.INFO)
    
    # 轮转文件handler
    fh = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8'
    )
    fh.setLevel(logging.INFO)
    
    # 控制台handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    
    formatter = logging.Formatter(
        '%(asctime)s [%(name)s] %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def simple_log(log_file: Path, msg: str, max_lines: int = 2000):
    """
    简单追加日志（带行数限制）
    超过max_lines时截断保留后半部分
    """
    log_file.parent.mkdir(exist_ok=True)
    
    # 写入新行
    with open(log_file, 'a', encoding='utf-8') as f:
        from datetime import datetime
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    
    # 检查行数并轮转
    try:
        lines = log_file.read_text(encoding='utf-8').splitlines()
        if len(lines) > max_lines:
            keep = lines[-(max_lines // 2):]
            log_file.write_text('\n'.join(keep) + '\n', encoding='utf-8')
    except Exception:
        pass

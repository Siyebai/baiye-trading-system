#!/usr/bin/env python3
"""
白夜守护 v9.0 — 不死守护
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
功能: 引擎进程监控 + 崩溃自启 + 心跳检测 + API监控
     内存告警 + 日志轮转 + 多实例防护
运行: python guardian_v90.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import os, signal, subprocess, sys, time, traceback
from datetime import datetime, timezone
from pathlib import Path
import requests
import msvcrt

PYTHON = sys.executable
ROOT = Path(__file__).parent
ENGINE = ROOT / "main_v90.py"
PID_FILE = ROOT / "data" / "baiye_v90.pid"
LOCK_FILE = ROOT / "data" / "guardian_v90.lock"
GUARDIAN_PID = ROOT / "data" / "guardian_v90.pid"
ENGINE_LOG = ROOT / "logs" / "baiye_v90.log"
GUARDIAN_LOG = ROOT / "logs" / "guardian_v90.log"

HEARTBEAT_TIMEOUT = 90
API_INTERVAL = 45
RESTART_MAX_HOUR = 6
BACKOFF = [5, 10, 20, 40, 60]
CHECK_INTERVAL = 10
REPORT_INTERVAL = 300
MEM_WARN = 400
MEM_KILL = 700
MAX_LOG_MB = 10


class Guardian:
    def __init__(self):
        for d in [ROOT/"logs", ROOT/"data"]: d.mkdir(parents=True, exist_ok=True)
        self._log = open(GUARDIAN_LOG, "a", encoding="utf-8")
        self._acquire_lock()
        self._clean_orphans()
        GUARDIAN_PID.write_text(str(os.getpid()))
        self.proc = None
        self.pid = None
        self.restarts = 0
        self.hour_start = time.time()
        self.last_hb = time.time()
        self.last_api = 0
        self.last_report = 0
        self.api_ok = True
        self.backoff_lv = 0
        self.score = 100

    def log(self, msg, tag="INFO"):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} [{tag}] {msg}"
        print(line, flush=True)
        self._log.write(line+"\n"); self._log.flush()

    def _acquire_lock(self):
        try:
            fd = open(LOCK_FILE, "w")
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            fd.write(str(os.getpid())); fd.flush()
            self._lock_fd = fd
        except (IOError, OSError):
            self.log("已有守护运行", "FATAL"); sys.exit(1)

    def _clean_orphans(self):
        if PID_FILE.exists():
            try: PID_FILE.unlink()
            except: pass

    def start(self):
        try:
            self.proc = subprocess.Popen(
                [PYTHON, str(ENGINE)], cwd=str(ROOT),
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=="win32" else 0)
            self.pid = self.proc.pid
            PID_FILE.write_text(str(self.pid))
            self.log(f"引擎启动 PID={self.pid}")
            return True
        except Exception as e:
            self.log(f"启动失败: {e}", "ERROR"); return False

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def heartbeat(self):
        if not ENGINE_LOG.exists(): return False
        try:
            alive = (time.time() - ENGINE_LOG.stat().st_mtime) < HEARTBEAT_TIMEOUT
            if alive: self.last_hb = time.time(); self.score = min(100, self.score+1)
            else: self.score = max(0, self.score-5)
            return alive
        except: return True

    def check_api(self):
        now = time.time()
        if now - self.last_api < API_INTERVAL: return self.api_ok
        self.last_api = now
        try:
            r = requests.get("https://testnet.binancefuture.com/fapi/v1/ping", timeout=8)
            self.api_ok = (r.status_code == 200)
            if not self.api_ok: self.score = max(0, self.score-10)
        except:
            self.api_ok = False; self.score = max(0, self.score-10)
        return self.api_ok

    def check_mem(self):
        try:
            import psutil
            if self.pid:
                m = psutil.Process(self.pid).memory_info().rss/(1024*1024)
                if m > MEM_KILL:
                    self.log(f"内存紧急:{m:.0f}MB", "FATAL"); return False, int(m)
                if m > MEM_WARN: self.log(f"内存警告:{m:.0f}MB", "WARN")
                return True, int(m)
        except: pass
        return True, 0

    def restart(self, reason):
        now = time.time()
        if now - self.hour_start >= 3600: self.restarts = 0; self.hour_start = now
        if self.restarts >= RESTART_MAX_HOUR:
            self.log(f"重启超限", "FATAL"); return False
        delay = BACKOFF[min(self.backoff_lv, len(BACKOFF)-1)]
        self.log(f"重启({reason}) 退避{delay}s [{self.restarts+1}/时]", "WARN")
        time.sleep(delay)
        if self.proc:
            try: self.proc.terminate(); time.sleep(2)
            except: pass
            if self.alive():
                try: self.proc.kill()
                except: pass
        if self.start():
            self.restarts += 1; self.backoff_lv = min(4, self.backoff_lv+1)
            return True
        return False

    def heal(self):
        # Clean caches
        for d in ROOT.glob("**/__pycache__"):
            import shutil
            try: shutil.rmtree(d, ignore_errors=True)
            except: pass
        # Log rotation
        if ENGINE_LOG.exists() and ENGINE_LOG.stat().st_size > MAX_LOG_MB*1024*1024:
            old = ENGINE_LOG.with_suffix(".log.old")
            with open(ENGINE_LOG, "rb") as f:
                f.seek(max(0, ENGINE_LOG.stat().st_size - 1024*1024))
                old.write_bytes(f.read())
            ENGINE_LOG.write_text("")
            self.log("日志轮转")

    def run(self):
        self.log("═"*50)
        self.log("白夜守护 v9.0 启动")
        self.log("═"*50)
        if not self.start(): return
        consecutive = 0
        while True:
            try:
                time.sleep(CHECK_INTERVAL)
                if not self.alive():
                    ec = self.proc.returncode if self.proc else -1
                    if ec == 0: self.log("正常退出"); break
                    if not self.restart(f"exit={ec}"): break
                    consecutive = 0; continue
                if not self.heartbeat():
                    self.log("心跳超时", "WARN")
                    if not self.restart("心跳"): break
                    continue
                self.check_api()
                ok, mem = self.check_mem()
                if not ok:
                    if not self.restart(f"内存{mem}MB"): break
                    continue
                if time.time() - self.last_report > REPORT_INTERVAL:
                    self.last_report = time.time(); self.heal()
                    self.log(f"引擎=OK API={'✅' if self.api_ok else '❌'} RAM:{mem}MB 重启:{self.restarts}/时 健康:{self.score}")
                    if self.score > 80 and self.backoff_lv > 0: self.backoff_lv -= 1
                consecutive = 0
            except KeyboardInterrupt: break
            except Exception as e:
                consecutive += 1
                self.log(f"异常(连续{consecutive}): {e}", "ERROR")
                if consecutive > 10: break
                time.sleep(5)
        self._shutdown()

    def _shutdown(self):
        if self.proc:
            try: self.proc.terminate(); time.sleep(3)
            except: pass
            try: self.proc.kill()
            except: pass
        PID_FILE.unlink(missing_ok=True)
        GUARDIAN_PID.unlink(missing_ok=True)
        if hasattr(self, '_lock_fd'):
            try:
                msvcrt.locking(self._lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
                self._lock_fd.close()
            except: pass
            LOCK_FILE.unlink(missing_ok=True)
        self._log.close()

if __name__ == "__main__":
    Guardian().run()

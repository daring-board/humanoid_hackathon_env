"""receiver（:8000）と Streamlit（:8501）を同時に起動する。Ctrl+C で両方止める。"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORTS = (8000, 8501)


def _pids_on_port(port: int) -> set[int]:
    """指定ポートを LISTEN しているプロセスの PID（Windows: netstat / POSIX: lsof）。"""
    if os.name == "nt":
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True).stdout
        return {int(m.group(1)) for line in out.splitlines()
                if "LISTENING" in line
                and (m := re.search(rf":{port}\s+\S+\s+LISTENING\s+(\d+)", line))}
    out = subprocess.run(["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                         capture_output=True, text=True).stdout
    return {int(p) for p in out.split()}


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        # /T で子プロセスごと落とす（terminate() は子孫を残す）
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
    else:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass


def free_ports() -> None:
    """前回の異常終了で残った孤児プロセスがポートを握っていたら止める。"""
    for port in PORTS:
        for pid in _pids_on_port(port) - {os.getpid()}:
            print(f"[run] port {port} is held by PID {pid} (stale) -> killing")
            _kill_tree(pid)


def main() -> None:
    free_ports()
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "receiver:app", "--app-dir", str(HERE),
             "--host", "0.0.0.0", "--port", "8000"]
        ),
        # headless: 初回のメールアドレス入力待ちで止まるのを防ぐ（ブラウザは下で開く）
        subprocess.Popen(
            [sys.executable, "-m", "streamlit", "run", str(HERE / "app.py"),
             "--server.port", "8501", "--server.headless", "true",
             "--browser.gatherUsageStats", "false"],
            stdin=subprocess.DEVNULL,
        ),
    ]
    threading.Timer(3.0, webbrowser.open, args=("http://localhost:8501",)).start()
    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                _kill_tree(p.pid)


if __name__ == "__main__":
    main()

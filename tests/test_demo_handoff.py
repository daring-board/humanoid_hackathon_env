#!/usr/bin/env python3
"""分類動作への引き渡し(phase1 の --notify-url / --then、next_action)と .env 読み込みのテスト。

pytest 不要: `python tests/test_demo_handoff.py`。一時ディレクトリだけを使い、外部通信・実機・実在の .env には触れない。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DEMO = SCRIPTS / "demo"
for p in (str(SCRIPTS), str(DEMO)):
    if p not in sys.path:
        sys.path.insert(0, p)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import commands  # noqa: E402
import env_file  # noqa: E402
import next_action  # noqa: E402
from common import new_state, now_iso, save_state  # noqa: E402
from make_qr import encode_qr  # noqa: E402

ENV = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
ENV["PYTHONIOENCODING"] = "utf-8"


def run(script, *args, timeout=120):
    """スクリプトを別プロセスで実行し (終了コード, 標準出力, 標準エラー) を返す。"""
    r = subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, env=ENV, cwd=str(ROOT))
    return r.returncode, r.stdout, r.stderr


def make_images(d: Path):
    qr = d / "box.png"
    cv2.imwrite(str(qr), encode_qr("B001:A"))
    blank = d / "blank.png"
    cv2.imwrite(str(blank), np.full((480, 640, 3), 128, np.uint8))
    return qr, blank


def phase1(d: Path, image: Path, *extra, timeout_sec=20):
    return run(DEMO / "phase1_read_qr.py", "--source", "file", "--path", image, "--no-show",
               "--timeout", timeout_sec, "--state", d / "state.json", "--log", d / "events.jsonl", *extra)


class _Sink(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).received.append(json.loads(body.decode("utf-8")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


@contextlib.contextmanager
def sink():
    _Sink.received = []
    srv = HTTPServer(("127.0.0.1", 0), _Sink)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/command", _Sink.received
    finally:
        srv.shutdown()
        srv.server_close()


# 子プロセスとして起動される小さなスクリプト: 受け取った引数と、その時点の read_qr の状態を書き出す
CHILD = """import json, sys
out, state, code = sys.argv[1], sys.argv[2], int(sys.argv[3])
st = json.load(open(state, encoding="utf-8"))
json.dump({"argv": sys.argv[4:], "read_qr": st["phases"]["read_qr"]["status"]}, open(out, "w", encoding="utf-8"))
sys.exit(code)
"""


def test_make_sort():
    c = commands.make_sort("B001", "A", "run1", verified=True, scanned_at="t")
    assert c["cmd"] == "sort" and c["box_id"] == "B001" and c["zone"] == "A"
    assert c["verified"] is True and c["run_id"] == "run1" and c["source"] == "recognition" and c["v"] == 1
    assert "+" in c["ts"] or c["ts"].endswith("Z")  # タイムゾーン付き


def test_notify_url():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        qr, _ = make_images(d)
        with sink() as (url, got):
            code, out, err = phase1(d, qr, "--notify-url", url)
        assert code == 0, err
        assert len(got) == 1 and got[0]["cmd"] == "sort" and got[0]["box_id"] == "B001" and got[0]["zone"] == "A"
        # 送信先が落ちていてもフェーズは成功する
        code, out, err = phase1(d, qr, "--notify-url", "http://127.0.0.1:9/command")
        assert code == 0, err
        st = json.loads((d / "state.json").read_text(encoding="utf-8"))
        assert st["phases"]["read_qr"]["status"] == "done"


def test_then_runs_after_state_saved():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        qr, blank = make_images(d)
        child, out_file = d / "child.py", d / "child_out.json"
        child.write_text(CHILD, encoding="utf-8")
        then = f'python "{child}" "{out_file}" {{state}} 7 {{zone}} {{box_id}} {{verified}}'
        code, out, err = phase1(d, qr, "--then", then)
        assert code == 7, (code, err)  # 子の終了コードがそのまま返る
        got = json.loads(out_file.read_text(encoding="utf-8"))
        assert got["argv"] == ["A", "B001", "null"], got
        assert got["read_qr"] == "done"  # 子が動いた時点で状態JSONは保存済み
        events = [json.loads(x) for x in (d / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert any("--then" in e.get("detail", "") and (e.get("data") or {}).get("exit_code") == 7 for e in events)
        # QR が読めなければ子は起動しない
        out_file.unlink()
        code, out, err = phase1(d, blank, "--then", then, timeout_sec=1)
        assert code == 1 and not out_file.exists()


def test_then_via_run_demo():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        qr, _ = make_images(d)
        child, out_file = d / "child.py", d / "child_out.json"
        child.write_text(CHILD, encoding="utf-8")
        then = f'python "{child}" "{out_file}" {{state}} 0 {{zone}} {{box_id}}'
        code, out, err = run(DEMO / "run_demo.py", "--to", "read_qr", "--skip", "approach", "--source", "file",
                             "--path", qr, "--no-show", "--timeout", 20, "--state", d / "state.json",
                             "--log", d / "events.jsonl", "--then", then)
        assert code == 0, (code, out[-400:], err[-400:])
        assert json.loads(out_file.read_text(encoding="utf-8"))["argv"] == ["A", "B001"]


def test_next_action():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        sp = d / "state.json"
        na = DEMO / "next_action.py"
        code, out, err = run(na, "--state", sp)  # 状態JSONなし
        assert code == 3 and out.strip() == ""
        st = new_state()
        save_state(st, sp)
        code, out, err = run(na, "--state", sp)  # read_qr が未完了
        assert code == 3 and out.strip() == ""
        assert next_action.read_next_action(sp) is None

        def mark_done(box_id, zone):
            s = json.loads(sp.read_text(encoding="utf-8"))
            s["box"] = {"box_id": box_id, "zone": zone, "scanned_at": now_iso(), "verified": None}
            s["phases"]["read_qr"]["status"] = "done"
            save_state(s, sp)

        mark_done("B001", "A")
        code, out, err = run(na, "--state", sp)
        got = json.loads(out)
        assert code == 0 and got["box_id"] == "B001" and got["zone"] == "A" and got["verified"] is None
        assert got["phase_status"]["read_qr"] == "done" and got["destination"] is None
        code, out, err = run(na, "--state", sp, "--field", "zone")
        assert code == 0 and out.strip() == "A"
        assert next_action.read_next_action(sp)["zone"] == "A"
        # --fresh は古い箱を返さない(時間切れ)
        t0 = time.monotonic()
        code, out, err = run(na, "--state", sp, "--fresh", "--wait", 1)
        assert code == 3 and out.strip() == "" and time.monotonic() - t0 >= 0.9
        # 待っている間に新しい箱が書かれたら、それを返す
        threading.Timer(1.0, mark_done, args=("B002", "B")).start()
        code, out, err = run(na, "--state", sp, "--fresh", "--wait", 10, "--field", "box_id")
        assert code == 0 and out.strip() == "B002", (code, out, err)
        # 壊れた状態JSON
        sp.write_text("{broken", encoding="utf-8")
        code, out, err = run(na, "--state", sp)
        assert code == 1 and out.strip() == ""


def test_env_file():
    secret = "SECRET-VALUE-123"
    keys = ("HH_T_A", "HH_T_B", "HH_T_C", "HH_T_D", "HH_T_KEEP")
    saved = {k: os.environ.pop(k, None) for k in keys}
    out, err = io.StringIO(), io.StringIO()
    try:
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / ".env"
            p.write_bytes(("﻿# コメント\n\nHH_T_A=" + secret + "\nexport HH_T_B = \"quoted value\"\n"
                           "HH_T_C='single'\nこれは壊れた行\nHH_T_D=\nHH_T_KEEP=from_file\n").encode("utf-8"))
            os.environ["HH_T_KEEP"] = "from_env"
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                loaded = env_file.load_env_file(p)
                missing = env_file.load_env_file(Path(t) / "nothing.env")
            assert sorted(loaded) == ["HH_T_A", "HH_T_B", "HH_T_C", "HH_T_D"], loaded  # 値ではなくキー名だけ
            assert missing == []
            assert os.environ["HH_T_A"] == secret and os.environ["HH_T_B"] == "quoted value"
            assert os.environ["HH_T_C"] == "single" and os.environ["HH_T_D"] == ""
            assert os.environ["HH_T_KEEP"] == "from_env"  # 既存の環境変数は上書きしない
            assert secret not in out.getvalue() and secret not in err.getvalue()
            assert secret not in repr(loaded)
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_default_run_unchanged():
    """新しい引数を付けない通し実行が、従来どおり最後まで done になる(dry-run)。"""
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        qr, _ = make_images(d)
        code, out, err = run(DEMO / "run_demo.py", "--skip", "approach", "--source", "file", "--path", qr,
                             "--no-show", "--timeout", 20, "--state", d / "state.json", "--log", d / "events.jsonl")
        assert code == 0, (code, out[-400:], err[-400:])
        st = json.loads((d / "state.json").read_text(encoding="utf-8"))
        assert [st["phases"][p]["status"] for p in ("read_qr", "grasp", "move", "release")] == ["done"] * 4
        assert st["destination"]["zone"] == "A"


TESTS = [test_make_sort, test_notify_url, test_then_runs_after_state_saved, test_then_via_run_demo,
         test_next_action, test_env_file, test_default_run_unchanged]


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    ok = 0
    for fn in TESTS:
        try:
            fn()
        except Exception:
            import traceback
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
        else:
            ok += 1
            print(f"PASS {fn.__name__}")
    print(f"{ok}/{len(TESTS)} 件成功")
    return 0 if ok == len(TESTS) else 1


if __name__ == "__main__":
    sys.exit(main())

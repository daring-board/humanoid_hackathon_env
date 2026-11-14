#!/usr/bin/env python3
"""認識を別スレッドで回し、担当Aのコードから使える形にした Recognizer。

使い方(担当Aのプロセス内):
    rec = Recognizer(source, on_box=lambda box_id, zone: ...)
    rec.start()
    box = rec.pop_box(timeout=1.0)     # {"box_id","zone","ts"} か None
    tgt = rec.get_target("A")          # 区画マーカーの最新観測 か None
    rec.stop()

DDS(ChannelFactoryInitialize)はここでは初期化しない。呼び出し側のプロセスで1回だけ行うこと。
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from commands import make_go_to, make_target, post_command
from events import make_event, post_event
from qr_reader import QRScanner, StableDecoder, locate, parse_payload, parse_zone_marker

LOST_AFTER_SEC = 1.0  # 区画マーカーを見失ったと扱うまでの秒数


def build_event(text, zones, run_id=None):
    """確定したQRテキストから scanned / error のイベントを作る。"""
    parsed = parse_payload(text)
    if parsed is None:
        return make_event(
            "error", detail=f"QRを解釈できません: {text!r}", run_id=run_id,
            error={"code": "UNKNOWN_QR", "message": "形式不正"}, data={"qr_raw": text})
    if parsed["zone"] not in zones:
        return make_event(
            "error", parsed["box_id"], None,  # 仕様: 定義外の zone は null で送る
            detail=f"想定外のzone {parsed['zone']!r}: {text!r}", run_id=run_id,
            error={"code": "UNKNOWN_QR", "message": f"zone {parsed['zone']} は想定外"},
            data={"qr_raw": text})
    return make_event(
        "scanned", parsed["box_id"], parsed["zone"],
        detail=f"QR読取成功 {parsed['box_id']} -> {parsed['zone']}", run_id=run_id,
        data={"qr_raw": text})


def build_verify_event(result, run_id=None):
    """GeminiVerifier の結果からイベントを作る。match -> verified、mismatch -> CONTENT_MISMATCH の error。
    skipped / error はイベントにしない(None)。"""
    status = result.get("status")
    res = result.get("result") or {}
    if status not in ("match", "mismatch"):
        return None
    data = {"observed": res.get("observed"), "confidence": res.get("confidence"),
            "model": result.get("model"), "elapsed_ms": result.get("elapsed_ms")}
    if status == "match":
        return make_event("verified", result["box_id"], result["zone"],
                          detail=f"見た目の照合OK: {res.get('observed')}", run_id=run_id, data=data)
    data["expected"] = result.get("expected")
    return make_event("error", result["box_id"], result["zone"],
                      detail=f"見た目が台帳と不一致: {res.get('observed')}", run_id=run_id,
                      error={"code": "CONTENT_MISMATCH", "message": res.get("reason")}, data=data)


class Recognizer:
    def __init__(self, source, zones=("A", "B"), server=None, command_url=None, on_box=None,
                 marker_size_m=None, hfov_deg=None, target_hz=2.0, *, run_id=None,
                 on_event=None, confirm_frames=3, cooldown_sec=5.0, verifier=None):
        """source: read()/close() を持つ取得元。on_event(ev, frame) は発行イベントごとに呼ばれる。
        verifier: 箱のQR確定時に見た目を照合する GeminiVerifier(任意)。"""
        self._source = source
        self.zones = {str(z).strip().upper() for z in zones if str(z).strip()}
        self.server = server
        self.command_url = command_url
        self.on_box = on_box
        self.on_event = on_event
        self.verifier = verifier
        if verifier is not None:
            verifier.on_result = self._on_verify
        self.marker_size_m = marker_size_m
        self.hfov_deg = hfov_deg
        self.target_interval = 1.0 / target_hz if target_hz and target_hz > 0 else 0.0
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M")
        self._decoder = StableDecoder(confirm_frames, cooldown_sec)
        self._scanner = QRScanner()  # 認識スレッド専用(フレームをまたいで追跡状態を持つ)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._queue = queue.Queue()
        self._last_box = None
        self._events = []
        self._obs = {}            # zone -> (monotonic時刻, locate結果)
        self._view = None         # (frame, detections)
        self._frames = 0
        self._times = deque(maxlen=30)
        self._last_frame_wall = None
        # go_to を出した区画への target 通知の状態
        self._goto_zone = None
        self._ref_time = 0.0      # go_to または最後に見えた時刻
        self._last_target_post = 0.0
        self._lost_sent = False
        self._warn_at = {}

    # ---- 起動・停止 ----
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="Recognizer", daemon=True)
        self._thread.start()

    def stop(self, close_source=False):
        """スレッドを止めて join する。source は close_source=True のときだけ閉じる。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        if self.verifier is not None:
            try:
                self.verifier.close()
            except Exception as e:
                print(f"[警告] 照合器の停止で例外: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        if close_source:
            self._source.close()

    # ---- 取得API ----
    def last_box(self):
        with self._lock:
            return dict(self._last_box) if self._last_box else None

    def pop_box(self, timeout=None):
        """未取得の箱を古い順に1件取り出す。無ければ None(timeout 秒待つ)。"""
        try:
            return self._queue.get(timeout=timeout) if timeout else self._queue.get_nowait()
        except queue.Empty:
            return None

    def get_target(self, zone, max_age=1.0):
        """区画マーカーの最新観測(locate の結果 + zone + age_sec)。max_age より古ければ None。"""
        zone = str(zone).strip().upper()
        with self._lock:
            ob = self._obs.get(zone)
        if ob is None:
            return None
        age = time.monotonic() - ob[0]
        if age > max_age:
            return None
        return {**ob[1], "zone": zone, "age_sec": round(age, 3)}

    def stats(self):
        with self._lock:
            ts = list(self._times)
            fps = (len(ts) - 1) / (ts[-1] - ts[0]) if len(ts) >= 2 and ts[-1] > ts[0] else 0.0
            return {"frames": self._frames, "fps": round(fps, 2),
                    "last_frame_time": self._last_frame_wall}

    def events(self):
        """これまでに発行したイベントのコピー。"""
        with self._lock:
            return list(self._events)

    def latest_view(self):
        """描画用: (最新フレーム, 検出リスト) か None。検出は
        {"kind": "box"|"zone", "text", "points", "zone", "loc"} の辞書。"""
        with self._lock:
            return self._view

    # ---- 内部 ----
    def _warn(self, key, msg, interval=3.0):
        now = time.monotonic()
        if now - self._warn_at.get(key, -1e9) >= interval:
            self._warn_at[key] = now
            print(msg, file=sys.stderr, flush=True)

    def _loop(self):
        last_ok = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                frame = self._source.read()
            except Exception as e:
                frame = None
                self._warn("read", f"[警告] フレーム取得で例外: {e}")
            if frame is None:
                if now - last_ok > 3.0:
                    self._warn("noframe", f"[警告] {now - last_ok:.0f}秒間フレームを取得できていません。"
                               "カメラ接続・取得元の設定を確認してください。")
                self._stop.wait(0.02)
                continue
            last_ok = now
            try:
                self._process(frame, now)
            except Exception as e:  # 認識スレッドは何があっても止めない
                self._warn("process", f"[警告] 認識処理で例外: {type(e).__name__}: {e}")
            self._stop.wait(0.003)

    def _process(self, frame, now):
        dets, box_texts, seen = [], [], {}
        box_points = {}
        for text, pts in self._scanner.scan(frame):
            zone = parse_zone_marker(text)
            if zone is not None:
                loc = locate(pts, frame.shape, self.marker_size_m, self.hfov_deg)
                dets.append({"kind": "zone", "text": text, "points": pts, "zone": zone, "loc": loc})
                if zone in self.zones and zone not in seen:
                    seen[zone] = loc
            else:
                box_texts.append(text)
                box_points.setdefault(text, pts)
                dets.append({"kind": "box", "text": text, "points": pts, "zone": None, "loc": None})
        with self._lock:
            self._frames += 1
            self._times.append(now)
            self._last_frame_wall = time.time()
            self._view = (frame, dets)
            for z, loc in seen.items():
                self._obs[z] = (now, loc)
        for text in self._decoder.update(box_texts, now=now):
            self._handle_box(text, frame, now, box_points.get(text))
        self._post_target(seen, now)

    def _on_verify(self, result):
        """照合結果(ワーカースレッドから呼ばれる)をイベントにして送る。"""
        try:
            ev = build_verify_event(result, self.run_id)
            if ev is None:
                if not result.get("quiet"):
                    print(f"[照合 {result.get('status')}] {result.get('box_id')}: {result.get('error')}",
                          file=sys.stderr, flush=True)
                return
            post_event(self.server, ev, timeout=1.0)
            with self._lock:
                self._events.append(ev)
                del self._events[:-1000]
        except Exception as e:
            print(f"[警告] 照合結果の処理で例外: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    def _handle_box(self, text, frame, now, points=None):
        ev = build_event(text, self.zones, self.run_id)
        post_event(self.server, ev, timeout=1.0)
        with self._lock:
            self._events.append(ev)
            del self._events[:-1000]
        if self.on_event:
            try:
                self.on_event(ev, frame)
            except Exception as e:
                print(f"[警告] on_event で例外: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        if ev["event"] != "scanned":
            return
        box = {"box_id": ev["box_id"], "zone": ev["zone"], "ts": time.time()}
        with self._lock:
            self._last_box = box
        self._queue.put(dict(box))
        if self.verifier is not None:
            try:
                self.verifier.submit(box["box_id"], box["zone"], frame.copy(), points)
            except Exception as e:  # 照合の失敗で認識スレッドを止めない
                print(f"[警告] 見た目の照合の依頼で例外(認識は継続): {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
        if self.on_box:
            try:
                self.on_box(box["box_id"], box["zone"])
            except Exception as e:
                print(f"[警告] on_box で例外(認識は継続): {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
        if self.command_url:
            post_command(self.command_url, make_go_to(box["box_id"], box["zone"], self.run_id))
            with self._lock:
                self._goto_zone = box["zone"]
                self._ref_time = now
                self._last_target_post = 0.0
                self._lost_sent = False

    def _post_target(self, seen, now):
        if not self.command_url:
            return
        with self._lock:
            zone = self._goto_zone
            if zone is None:
                return
            if zone in seen:
                if now - self._last_target_post < self.target_interval:
                    return
                self._last_target_post = now
                self._ref_time = now
                self._lost_sent = False
                cmd = make_target(zone, seen[zone], self.run_id)
            elif now - self._ref_time >= LOST_AFTER_SEC and not self._lost_sent:
                self._lost_sent = True
                cmd = make_target(zone, None, self.run_id)
            else:
                return
        post_command(self.command_url, cmd)


# ---- recognize.py / control_example.py 共通の引数 ----

def add_recognizer_args(parser) -> None:
    parser.add_argument("--server", type=str, default=None,
                        help="イベント送信先(例 http://localhost:8000/events)。未指定なら出力のみ")
    parser.add_argument("--zones", type=str, default="A,B", help="想定する zone(カンマ区切り)")
    parser.add_argument("--command-url", type=str, default=None,
                        help="担当Aへの指示の送信先(例 http://localhost:8001/command)")
    parser.add_argument("--marker-size-cm", type=float, default=None,
                        help="区画マーカーQRの一辺(cm)。--hfov-deg と併用で距離を推定")
    parser.add_argument("--hfov-deg", type=float, default=None, help="カメラの水平画角(度)")
    parser.add_argument("--target-hz", type=float, default=2.0, help="target 指示の送信上限(Hz)")
    parser.add_argument("--gemini", action="store_true",
                        help="箱のQR確定時に Gemini で見た目を台帳と照合する(要 環境変数 GEMINI_API_KEY)")
    parser.add_argument("--catalog", type=str, default=str(Path(__file__).resolve().parent / "catalog.json"),
                        help="見た目の台帳 JSON(--gemini 用)")
    parser.add_argument("--gemini-model", type=str, default=None,
                        help="Gemini のモデル名(既定: 環境変数 GEMINI_MODEL、無ければ gemini-3.5-flash-lite)")
    parser.add_argument("--gemini-timeout", type=float, default=20.0, help="Gemini の HTTP タイムアウト(秒)")
    parser.add_argument("--gemini-endpoint", type=str, default=None, help="Gemini のエンドポイント(テスト用)")


def recognizer_from_args(args, source, **extra):
    zones = [z for z in str(getattr(args, "zones", "A,B")).split(",") if z.strip()]
    cm = getattr(args, "marker_size_cm", None)
    if getattr(args, "gemini", False) and "verifier" not in extra:
        from gemini_verify import GeminiVerifier, load_catalog
        try:
            catalog = load_catalog(args.catalog)
        except (OSError, ValueError) as e:
            print(f"[警告] 台帳を読めません({e})。見た目の照合は行いません。", file=sys.stderr, flush=True)
            catalog = {}
        extra["verifier"] = GeminiVerifier(
            catalog, model=getattr(args, "gemini_model", None), endpoint=getattr(args, "gemini_endpoint", None),
            timeout=getattr(args, "gemini_timeout", 20.0))
    return Recognizer(
        source, zones=zones, server=getattr(args, "server", None),
        command_url=getattr(args, "command_url", None),
        marker_size_m=cm / 100.0 if cm else None, hfov_deg=getattr(args, "hfov_deg", None),
        target_hz=getattr(args, "target_hz", 2.0) or 2.0, **extra)

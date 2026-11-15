#!/usr/bin/env python3
"""カメラ取得元の切り替え層。

open_source(kind, **opts) が返すオブジェクトは次の2つを持つ。
  read()  -> np.ndarray | None   BGR画像。取れなければ None
  close() -> None

kind: webcam / file / videohub / udp
unitree_sdk2py は videohub を使うときだけ関数内で import する。
"""
from __future__ import annotations

import collections
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# udp(RTP/H264) を FFmpeg で開くための許可設定。cv2 の import より前に入れる必要がある。
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "protocol_whitelist;file,udp,rtp")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

SOURCES = ("webcam", "file", "videohub", "udp")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}


class CameraError(RuntimeError):
    """取得元を開けなかったときの例外（原因の見当を日本語で添える）。"""


class _Base:
    def read(self):  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:
        pass


class WebcamSource(_Base):
    def __init__(self, index: int = 0):
        cap = None
        if sys.platform.startswith("win") and hasattr(cv2, "CAP_DSHOW"):
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            if not cap.isOpened():
                cap.release()
                cap = None
        if cap is None:
            cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            cap.release()
            raise CameraError(
                f"Webカメラ(index={index})を開けません。接続・他アプリによる占有・"
                f"index の番号(--index 1 など)・カメラのプライバシー設定を確認してください。"
            )
        self._cap = cap

    def read(self):
        ok, frame = self._cap.read()
        return frame if ok else None

    def close(self) -> None:
        self._cap.release()


class FileSource(_Base):
    """画像ならずっと同じ画像、動画なら順に返して末尾でループする。"""

    def __init__(self, path):
        if not path:
            raise CameraError("file 取得元には --path が必要です。")
        p = Path(path)
        if not p.exists():
            raise CameraError(f"ファイルが見つかりません: {p}")
        self._image = None
        self._cap = None
        if p.suffix.lower() in IMAGE_EXTS:
            # 日本語パス対策で imdecode を使う
            img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                raise CameraError(f"画像を読み込めません(壊れた画像?): {p}")
            self._image = img
        else:
            cap = cv2.VideoCapture(str(p))
            if not cap.isOpened():
                cap.release()
                raise CameraError(f"動画を開けません(コーデック未対応?): {p}")
            self._cap = cap

    def read(self):
        if self._image is not None:
            return self._image.copy()
        ok, frame = self._cap.read()
        if not ok:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
        return frame if ok else None

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()


JPEG_EOI_TAIL_MAX = 64  # EOI(FFD9) が末尾からこのバイト数以内にあるものだけ正常とみなす
DROP_WINDOW = 50        # 捨てた割合を見る直近の枚数
DROP_WARN_RATIO = 0.30  # これを超えたら警告
DROP_WARN_INTERVAL = 3.0


def clean_jpeg(data) -> bytes | None:
    """videohub の JPEG を decode 前に検査する。
    SOI(FFD8)で始まり、末尾付近(最大 JPEG_EOI_TAIL_MAX バイト以内)に EOI(FFD9)があれば、
    EOI までに切り詰めたバイト列を返す(余分な末尾バイトを除く)。満たさなければ None(捨てる)。"""
    data = bytes(data)
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    idx = data.rfind(b"\xff\xd9")
    if idx < 2 or len(data) - (idx + 2) > JPEG_EOI_TAIL_MAX:
        return None
    return data[:idx + 2]


class VideoHubSource(_Base):
    """DDS 経由の画像取得。DDS は source_from_args が ensure_dds で初期化済みであること。
    欠けた・余分なバイトのある JPEG は decode 前に検査し、壊れたものは捨てる(None を返す)。"""

    def __init__(self, clock=time.monotonic):
        try:
            from unitree_sdk2py.go2.video.video_client import VideoClient
        except ImportError as e:  # SDK が無い環境
            raise CameraError(
                f"unitree_sdk2py を import できません({e})。pixi 環境で実行してください。"
            ) from e
        self._client = VideoClient()
        self._client.SetTimeout(3.0)
        self._client.Init()
        self._clock = clock
        self._last_warn = 0.0
        self._last_drop_warn = -1e9
        self._dropped = 0
        self._decoded = 0
        self._recent = collections.deque(maxlen=DROP_WINDOW)  # True=捨てた

    def stats(self) -> dict:
        total = self._dropped + self._decoded
        return {"decoded": self._decoded, "dropped": self._dropped,
                "drop_ratio": (self._dropped / total) if total else 0.0}

    def _note(self, dropped: bool) -> None:
        self._recent.append(dropped)
        if dropped:
            self._dropped += 1
        else:
            self._decoded += 1
        n = len(self._recent)
        if n >= 10 and sum(self._recent) / n > DROP_WARN_RATIO:
            now = self._clock()
            if now - self._last_drop_warn >= DROP_WARN_INTERVAL:
                self._last_drop_warn = now
                print(f"[videohub] 直近 {n} 枚のうち {sum(self._recent)} 枚の JPEG が壊れていて捨てました"
                      "(回線・ロボット側の映像配信の負荷を確認)", file=sys.stderr, flush=True)

    def read(self):
        code, data = self._client.GetImageSample()
        if code != 0:
            now = self._clock()
            if now - self._last_warn > 3.0:
                print(f"[videohub] GetImageSample エラーコード: {code}"
                      "(DDS接続・-i のインターフェース名・ロボット側の映像サービスを確認)",
                      file=sys.stderr)
                self._last_warn = now
            return None
        jpeg = clean_jpeg(data)
        if jpeg is None:
            self._note(True)
            return None
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        self._note(img is None)
        return img


class UdpSource(_Base):
    """R1 の RTP/H264 ストリーム。5001=既定、5002=左目、5003=右目。
    遅延がたまらないよう、バックグラウンドで読み続けて最新フレームだけ保持する。"""

    def __init__(self, port: int = 5001):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="r1sdp_")
        sdp_path = Path(self._tmpdir.name) / "r1.sdp"
        sdp_path.write_text(
            "v=0\n"
            "o=- 0 0 IN IP4 127.0.0.1\n"
            "s=R1\n"
            "c=IN IP4 0.0.0.0\n"
            "t=0 0\n"
            f"m=video {port} RTP/AVP 96\n"
            "a=rtpmap:96 H264/90000\n",
            encoding="ascii",
        )
        cap = cv2.VideoCapture(str(sdp_path), cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap.release()
            self._tmpdir.cleanup()
            raise CameraError(
                f"UDP ストリーム(ポート {port})を開けません。OpenCV が FFmpeg 付きか、"
                f"ポート {port} がファイアウォールで塞がれていないか、"
                f"ロボットが送信先PCへ配信しているかを確認してください。"
            )
        self._cap = cap
        self._lock = threading.Lock()
        self._frame = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if ok:
                with self._lock:
                    self._frame = frame
            else:
                time.sleep(0.01)

    def read(self):
        with self._lock:
            frame, self._frame = self._frame, None  # 同じフレームを2度返さない
        return frame

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._cap.release()
        self._tmpdir.cleanup()


def open_source(kind: str, **opts):
    """取得元を開く。opts: index(webcam), path(file), port(udp)。"""
    if kind == "webcam":
        return WebcamSource(int(opts.get("index") or 0))
    if kind == "file":
        return FileSource(opts.get("path"))
    if kind == "videohub":
        return VideoHubSource()
    if kind == "udp":
        return UdpSource(int(opts.get("port") or 5001))
    raise CameraError(f"未知の取得元です: {kind}(候補: {', '.join(SOURCES)})")


# ---- recognize.py / camera_check.py 共通の補助 ----

def add_source_args(parser) -> None:
    """取得元まわりの共通引数を追加する。"""
    parser.add_argument("--source", choices=SOURCES, default="webcam", help="カメラ取得元")
    parser.add_argument("--index", type=int, default=0, help="webcam のカメラ番号")
    parser.add_argument("--path", type=str, default=None, help="file 取得元の画像/動画パス")
    parser.add_argument("--port", type=int, default=5001,
                        help="udp のポート(5001=既定, 5002=左目, 5003=右目)")
    parser.add_argument("-i", "--interface", type=str, default=None,
                        help="ネットワークインターフェース(videohub 用)")
    parser.add_argument("-d", "--domain", type=int, default=0, help="DDS ドメインID(videohub 用)")


ROBOT_IP = "192.168.123.161"  # README「IP アドレス設計」: ロボット(固定)
PC_IP = "192.168.123.164"     # README「IP アドレス設計」: 開発用PC(有線LANに手動設定)


def check_network() -> bool:
    """README の前提(PC=192.168.123.164 を有線LANに設定、ロボット=192.168.123.161)を確かめる。
    問題があれば警告を出すだけで、処理は止めない。"""
    import socket
    import subprocess

    ok = True
    try:
        local = {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        local = set()
    if PC_IP not in local:
        ok = False
        print(f"[警告] このPCに {PC_IP} が設定されていません(現在: {sorted(local)})。"
              "README「3. ロボットとの接続方法」のとおり有線LANに手動設定してください。", file=sys.stderr)
    count = ["-n", "1", "-w", "1000"] if sys.platform == "win32" else ["-c", "1", "-W", "1"]
    try:
        reachable = subprocess.run(["ping", *count, ROBOT_IP], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        reachable = False
    if not reachable:
        ok = False
        print(f"[警告] ロボット {ROBOT_IP} に ping が通りません。LANケーブルと電源を確認してください。",
              file=sys.stderr)
    return ok


def source_from_args(args):
    """引数から取得元を生成する。videohub のときだけ DDS を1回初期化する。"""
    if args.source in ("videohub", "udp"):
        check_network()
    if args.source == "videohub":
        from dds_util import DdsError, ensure_dds
        try:
            ensure_dds(args.domain, args.interface)
        except DdsError as e:
            raise CameraError(str(e)) from e
    return open_source(args.source, index=args.index, path=args.path, port=args.port)

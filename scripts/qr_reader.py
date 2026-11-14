#!/usr/bin/env python3
"""QR 検出・デコード、ペイロード解釈、連続フレームでの確定判定。"""
from __future__ import annotations

import json
import math
import re
import threading
import time

import cv2
import numpy as np


def _normalize(ret):
    """detectAndDecodeMulti の戻り値の版差を吸収して (texts, points) にする。"""
    if ret is None:
        return [], None
    if len(ret) == 4:
        ok, texts, points, _ = ret
        if not ok:
            return [], None
    elif len(ret) == 3:
        texts, points, _ = ret
    else:
        return [], None
    if points is None or texts is None:
        return [], None
    return list(texts), points


def _try(detector, img):
    try:
        texts, points = _normalize(detector.detectAndDecodeMulti(img))
    except cv2.error:
        return []
    out = []
    for i, t in enumerate(texts):
        if t:  # 空文字=検出できたが読めなかった
            out.append((t, np.asarray(points[i], dtype=np.float32).reshape(-1, 2)))
    return out


_local = threading.local()


def _detectors():
    """スレッドごとに1回だけ作る検出器 {"aruco": ... または無し, "classic": ...}。
    detectAndDecodeMulti は内部状態を持つので、スレッドをまたいで共有しない。"""
    dets = getattr(_local, "dets", None)
    if dets is None:
        dets = {}
        if hasattr(cv2, "QRCodeDetectorAruco"):
            try:
                dets["aruco"] = cv2.QRCodeDetectorAruco()
            except Exception:
                pass
        dets["classic"] = cv2.QRCodeDetector()
        _local.dets = dets
    return dets


def _fast_det():
    """等倍の高速パスに使う検出器(実測で最も速く当たりやすい Aruco 版。無ければ従来版)。"""
    d = _detectors()
    return d.get("aruco") or d["classic"]


def _gray(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def _sharpen(gray):
    """アンシャープマスク。ぼけた小さい QR の境界を立てる(実測で拡大読取の当たりが増える)。"""
    blur = cv2.GaussianBlur(gray, (0, 0), 2)
    return cv2.addWeighted(gray, 2.0, blur, -1.0, 0)


def _tile_rects(width, height, nx, ny, overlap=0.25):
    """画像を nx*ny に分け、各タイルを overlap(タイル幅の割合)ずつ広げた矩形 (x0,y0,x1,y1) の一覧。"""
    out = []
    tw, th = width / nx, height / ny
    for j in range(ny):
        for i in range(nx):
            out.append((max(0, int(i * tw - tw * overlap)), max(0, int(j * th - th * overlap)),
                        min(width, int((i + 1) * tw + tw * overlap)),
                        min(height, int((j + 1) * th + th * overlap))))
    return out


def _read_region(det, gray, rect, scale, sharpen=True):
    """gray の rect を scale 倍に拡大して読み、座標を元画像に戻して返す。"""
    x0, y0, x1, y1 = rect
    if x1 - x0 < 8 or y1 - y0 < 8:
        return []
    crop = gray[y0:y1, x0:x1]
    if scale != 1.0:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    if sharpen:
        crop = _sharpen(crop)
    return [(t, p / scale + np.array([x0, y0], dtype=np.float32)) for t, p in _try(det, crop)]


# 重い再試行の種類。(名前, 検出器キー, 分割(nx,ny), 拡大率)。1フレームにつき1種類の1タイルだけ回す。
# 実測(1280x720、ぼかし+JPEG70): 見かけ幅60〜100pxの QR を拾えるのは「アンシャープ+タイル拡大」。
# 検出器は拡大率・ピクセルの並びで当たり外れが大きいので、拡大率を変えた全体読みも混ぜる。
# 適応的二値化・CLAHE は 200ms 超かかる割に当たりが増えないので外した。
_RETRY_STAGES = (
    ("full_a15", "aruco", (1, 1), 1.5),   # 全体を1.5倍。大きめ(100〜200px)の取りこぼしに効く
    ("full_c2", "classic", (1, 1), 2.0),
    ("full_a1", "aruco", (1, 1), 1.0),    # 全体を等倍のままアンシャープだけ
    ("tile_c2", "classic", (3, 2), 2.0),
    ("tile_a2", "aruco", (3, 2), 2.0),
    ("tile_c3", "classic", (3, 2), 3.0),
)


def _stage_det(key):
    d = _detectors()
    return d.get(key) or d["classic"]


def _dedup(found):
    seen, out = set(), []
    for t, p in found:
        if t not in seen:
            seen.add(t)
            out.append((t, p))
    return out


class QRScanner:
    """フレームをまたいで状態を持つ QR 読取。scan(frame) -> [(text, points(4x2, 元画像座標)), ...]。

    1. 追跡: 直前に読めた QR の周辺(外接矩形の2倍)だけを切り出して拡大して読む。速く小さい QR に強い。
    2. 高速パス: 等倍で検出器1つ。追跡で読めていないフレームは毎回、読めている間は4フレームに1回(新しい QR を探す)。
    3. 0件なら重い再試行を1フレームにつき1種類(1タイル)だけ順に回す。当たった種類は次回最初に試す。
    last_stage: 今回読めた段("track"/"fast"/再試行名/None)。stats(): 段ごとの試行・ヒット・平均ms。"""

    TRACK_SIDE_PX = 200.0   # 追跡時、QR の辺がこの長さになるよう拡大する
    TRACK_MAX_SCALE = 6.0
    TRACK_MAX = 4
    RETRY_BUDGET_MS = 100.0  # 再試行1回の平均がこれを超える種類は間引く
    RETRY_HEAVY_EVERY = 6
    FAST_EVERY = 4          # 追跡で読めている間の、全体パスの間引き間隔(フレーム)

    def __init__(self):
        self.last_stage = None
        self._frame_no = 0
        self._skipped = {}
        self._tracks = []           # 直前フレームで読めた QR の points
        self._retry_order = [s[0] for s in _RETRY_STAGES]
        self._tile_idx = {s[0]: 0 for s in _RETRY_STAGES}
        self._stats = {}

    def _rec(self, stage, ms, hit):
        s = self._stats.setdefault(stage, [0, 0, 0.0])
        s[0] += 1
        s[1] += 1 if hit else 0
        s[2] += ms

    def stats(self):
        """{段名: {"tries": 試行回数, "hits": ヒット回数, "ms_mean": 平均ms}}"""
        return {k: {"tries": v[0], "hits": v[1], "ms_mean": v[2] / v[0] if v[0] else 0.0}
                for k, v in self._stats.items()}

    def reset(self):
        self._tracks = []

    def _track(self, gray):
        h, w = gray.shape[:2]
        found = []
        for pts in self._tracks:
            side = float(np.mean([np.linalg.norm(pts[i] - pts[(i + 1) % 4]) for i in range(4)]))
            if side < 4:
                continue
            x0, y0 = pts.min(axis=0)
            x1, y1 = pts.max(axis=0)
            cx, cy, hw, hh = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0), (y1 - y0)  # 2倍に広げる=半幅は元の幅
            rect = (max(0, int(cx - hw)), max(0, int(cy - hh)), min(w, int(cx + hw)), min(h, int(cy + hh)))
            scale = min(self.TRACK_MAX_SCALE, max(1.0, self.TRACK_SIDE_PX / side))
            for det in (_fast_det(), _detectors()["classic"]):
                r = _read_region(det, gray, rect, scale, sharpen=True) or                     _read_region(det, gray, rect, scale, sharpen=False)
                if r:
                    found += r
                    break
        return _dedup(found)

    def scan(self, frame):
        self.last_stage = None
        if frame is None:
            self._tracks = []
            return []
        gray = _gray(frame)
        found = []
        if self._tracks:
            t0 = time.perf_counter()
            found = self._track(gray)
            self._rec("track", (time.perf_counter() - t0) * 1000.0, bool(found))
            if found:
                self.last_stage = "track"
        self._frame_no += 1
        # 追跡で読めている間は、新しい QR を探す全体パスを FAST_EVERY フレームに1回に間引く
        # (QR が写っていると全体パスが数百msかかることがあり、追跡だけなら数十msで済むため)。
        if not found or self._frame_no % self.FAST_EVERY == 0:
            t0 = time.perf_counter()
            fast = _try(_fast_det(), frame)
            self._rec("fast", (time.perf_counter() - t0) * 1000.0, bool(fast))
            if fast:
                if not found:
                    self.last_stage = "fast"
                found = _dedup(found + fast)
        if not found:
            found = self._retry(gray)
        self._tracks = [p for _, p in found][:self.TRACK_MAX]
        return found

    def _retry(self, gray):
        # 平均が RETRY_BUDGET_MS を超える重い種類(模様の多い背景で起きる)は RETRY_HEAVY_EVERY 回に1回だけ回す
        name = None
        for _ in range(len(self._retry_order)):
            cand = self._retry_order[0]
            self._retry_order = self._retry_order[1:] + self._retry_order[:1]
            st = self._stats.get(cand)
            heavy = st is not None and st[0] >= 2 and st[2] / st[0] > self.RETRY_BUDGET_MS
            if heavy:
                self._skipped[cand] = self._skipped.get(cand, 0) + 1
                if self._skipped[cand] < self.RETRY_HEAVY_EVERY:
                    continue
                self._skipped[cand] = 0
            name = cand
            break
        if name is None:
            return []
        spec = next(s for s in _RETRY_STAGES if s[0] == name)
        _, key, grid, scale = spec
        rects = _tile_rects(gray.shape[1], gray.shape[0], *grid)
        i = self._tile_idx[name]
        self._tile_idx[name] = (i + 1) % len(rects)
        t0 = time.perf_counter()
        r = _read_region(_stage_det(key), gray, rects[i], scale)
        self._rec(name, (time.perf_counter() - t0) * 1000.0, bool(r))
        if r:
            self.last_stage = name
            # 当たった種類を次回最初に試し、同じタイルから再開する
            self._retry_order.remove(name)
            self._retry_order.insert(0, name)
            self._tile_idx[name] = i
        return _dedup(r)


def decode_qr(frame):
    """frame(BGR) から QR を読む徹底版。戻り値は [(text, points(4x2, 元画像座標)), ...]。
    等倍で検出器2種、0件なら重い再試行(全種類・全タイル)を順に全部試す。遅い(QRなしで数百ms)ので
    ライブ用は QRScanner を使うこと。"""
    if frame is None:
        return []
    dets = _detectors()
    for det in dets.values():
        r = _try(det, frame)
        if r:
            return r
    gray = _gray(frame)
    h, w = gray.shape[:2]
    for _, key, grid, scale in _RETRY_STAGES:
        det = _stage_det(key)
        for rect in _tile_rects(w, h, *grid):
            r = _read_region(det, gray, rect, scale)
            if r:
                return _dedup(r)
    return []


_SIMPLE = re.compile(r"^\s*([A-Za-z0-9_\-]+)\s*:\s*([A-Za-z0-9_\-]+)\s*$")


_ZONE_MARKER = re.compile(r"^\s*ZONE\s*:\s*([A-Za-z0-9_\-]+)\s*$", re.IGNORECASE)


def parse_zone_marker(text):
    """区画マーカー(`ZONE:A` または {"zone_marker":"A"})なら大文字化した zone を返す。違えば None。"""
    if not isinstance(text, str):
        return None
    s = text.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        zone = obj.get("zone_marker")
        if not isinstance(zone, str) or not zone.strip():
            return None
        return zone.strip().upper()
    m = _ZONE_MARKER.match(s)
    return m.group(1).upper() if m else None


# 現場の箱に貼ってある QR の中身(ITEM-xxx)から、箱IDと区画を引く固定の表。キーは大文字・前後空白なし。
# ITEM と ITME は同じ QR の綴り違い(仕様メモの表記ゆれ)で、どちらで貼ってあっても同じ扱いにする。
FIXED_QR_TABLE = {
    "ITEM-001": ("A001", "A"),
    "ITEM-002": ("B001", "B"),
    "ITEM-003": ("B001", "B"),
}
FIXED_QR_TABLE.update({k.replace("ITEM", "ITME"): v for k, v in list(FIXED_QR_TABLE.items())})


def parse_payload(text):
    """{"box_id":"B001","zone":"A"} / B001:A / 固定表(FIXED_QR_TABLE の ITEM-001 など)を解釈する。不可なら None。
    区画マーカー(ZONE:A など)は箱ではないので None。"""
    if not isinstance(text, str):
        return None
    if parse_zone_marker(text) is not None:
        return None
    s = text.strip()
    fixed = FIXED_QR_TABLE.get(s.upper())
    if fixed is not None:
        return {"box_id": fixed[0], "zone": fixed[1]}
    if s.startswith("{"):
        try:
            obj = json.loads(s)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        box, zone = obj.get("box_id"), obj.get("zone")
    else:
        m = _SIMPLE.match(s)
        if not m:
            return None
        box, zone = m.group(1), m.group(2)
    if not isinstance(box, str) or not isinstance(zone, str):
        return None
    box, zone = box.strip(), zone.strip().upper()
    if not box or not zone:
        return None
    return {"box_id": box, "zone": zone}


def locate(points, frame_shape, marker_size_m=None, hfov_deg=None):
    """QRの4隅(4x2, 画素)から画像内の位置を出す。
    offset: 画像中心からの水平ずれを -1.0〜+1.0 に正規化(右が正)。
    bearing: "left"/"center"/"right"(|offset|<=0.15 を center)。
    size_ratio: QRの辺の平均長 / 画像幅。
    distance_m: marker_size_m と hfov_deg が両方あるときだけ、ピンホール近似
      marker_size_m / (2*tan(hfov/2)*size_ratio) で推定。無ければ None。"""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    width = float(frame_shape[1])
    cx = float(pts[:, 0].mean())
    offset = max(-1.0, min(1.0, (cx - width / 2.0) / (width / 2.0)))
    sides = [float(np.linalg.norm(pts[i] - pts[(i + 1) % len(pts)])) for i in range(len(pts))]
    size_ratio = (sum(sides) / len(sides)) / width
    if abs(offset) <= 0.15:
        bearing = "center"
    else:
        bearing = "right" if offset > 0 else "left"
    distance = None
    if marker_size_m and hfov_deg and size_ratio > 0 and 0 < hfov_deg < 180:
        distance = float(marker_size_m) / (2.0 * math.tan(math.radians(hfov_deg) / 2.0) * size_ratio)
    return {
        "offset": round(offset, 4),
        "bearing": bearing,
        "size_ratio": round(size_ratio, 4),
        "distance_m": None if distance is None else round(distance, 3),
    }


class StableDecoder:
    """同じテキストが window_sec 以内に confirm_frames 回読めたら確定(間に読めないフレームが挟まってもよい)。
    確定後 cooldown_sec は同じテキストを返さない。
    実機はフレームレートが低く(数fps)読み落としもあるので、連続フレームではなく時間窓で数える。"""

    def __init__(self, confirm_frames: int = 3, cooldown_sec: float = 5.0, window_sec: float = 2.0,
                 clock=time.monotonic):
        self.confirm_frames = confirm_frames
        self.cooldown_sec = cooldown_sec
        self.window_sec = window_sec
        self._clock = clock
        self._seen = {}   # text -> 窓内で見えた時刻の一覧
        self._last = {}

    def update(self, texts, now=None):
        """1フレーム分のテキスト一覧を渡し、新たに確定したテキストのリストを返す。"""
        now = self._clock() if now is None else now
        seen = set(texts)
        cutoff = now - self.window_sec
        for t in list(self._seen):
            self._seen[t] = [x for x in self._seen[t] if x >= cutoff]
            if not self._seen[t] and t not in seen:
                del self._seen[t]
        confirmed = []
        for t in sorted(seen):
            self._seen.setdefault(t, []).append(now)
            if len(self._seen[t]) < self.confirm_frames:
                continue
            last = self._last.get(t)
            if last is not None and now - last < self.cooldown_sec:
                continue
            self._last[t] = now
            self._seen[t] = []
            confirmed.append(t)
        return confirmed

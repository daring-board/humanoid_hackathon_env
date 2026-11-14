#!/usr/bin/env python3
"""箱の見た目を Gemini API で照合する(分類先の判定はQR。これは追加の確認)。

QRが確定した瞬間の画像を Gemini に送り、台帳(catalog.json)に書かれた見た目と合っているかを判定する。
遅い・失敗する・キーが無い場合でも認識と移動は止めない(ワーカースレッドで非同期に処理する)。
依存は標準ライブラリ(urllib)と numpy / cv2 のみ。API キーは環境変数 GEMINI_API_KEY(無ければリポジトリ直下の .env)からだけ読み、
コード・ログ・例外・イベント・保存ファイルには出さない(ヘッダで渡す)。

単体で試す:
  python scripts/gemini_verify.py --image work/qr/B001_A.png --box-id B001 --dry-run
  python scripts/gemini_verify.py --image photo.jpg --box-id B001 --expected "赤いラベルの箱"
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

import cv2
import numpy as np

DEFAULT_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
API_REVISION = "2026-05-20"
DEFAULT_CATALOG = str(Path(__file__).resolve().parent / "catalog.json")

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "match": {"type": "boolean", "description": "期待する見た目と一致するか"},
        "confidence": {"type": "number", "description": "確信度 0〜1"},
        "observed": {"type": "string", "description": "見えた特徴(日本語で短く)"},
        "reason": {"type": "string", "description": "判定の理由"},
    },
    "required": ["match", "confidence", "observed", "reason"],
}


class GeminiError(Exception):
    """Gemini 呼び出し・応答解釈の失敗。メッセージに API キーは含めない。"""


def load_catalog(path) -> dict:
    """台帳 JSON を読む。形: {"B001": {"zone": "A", "expected": "..."}, ...}"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"台帳の形式が不正です(オブジェクトではありません): {path}")
    return data


# ---- 画像 ----

def crop_around(frame, points, scale: float = 3.0, max_side: int = 768):
    """QRの4隅を囲む矩形を中心に scale 倍へ広げて切り出す(画像外へははみ出さない)。
    長辺が max_side を超えたら縮小する。points が None ならフレーム全体を縮小だけ。
    周囲の人の写り込みと送信量を減らす目的。"""
    h, w = frame.shape[:2]
    if points is None:
        img = frame
    else:
        pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        hw, hh = (x1 - x0) * scale / 2.0, (y1 - y0) * scale / 2.0
        l = int(max(0, np.floor(cx - hw)))
        t = int(max(0, np.floor(cy - hh)))
        r = int(min(w, np.ceil(cx + hw)))
        b = int(min(h, np.ceil(cy + hh)))
        if r - l < 1 or b - t < 1:  # 範囲外・潰れた矩形はフレーム全体にフォールバック
            l, t, r, b = 0, 0, w, h
        img = frame[t:b, l:r]
    ih, iw = img.shape[:2]
    longest = max(ih, iw)
    if longest > max_side:
        k = max_side / float(longest)
        img = cv2.resize(img, (max(1, int(round(iw * k))), max(1, int(round(ih * k)))),
                         interpolation=cv2.INTER_AREA)
    return img


def build_request(image_bgr, box_id, expected, model) -> dict:
    """Interactions API の送信本文を作る(テキスト→画像の順)。"""
    ok, buf = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise GeminiError("JPEG エンコードに失敗しました")
    b64 = base64.b64encode(buf.tobytes()).decode("ascii")
    prompt = (
        "物流ロボットの箱確認です。画像の中央付近にある、QRコードが付いた箱を見て、"
        f"期待する見た目『{expected}』と一致するか判定してください。"
        "QRコード自体の内容は判定に使わないでください。"
        "画像から見えないものは推測せず、見えた範囲だけで判断してください。"
        f"(箱ID: {box_id})"
        "match は一致なら true、confidence は 0〜1、observed は見えた特徴を日本語で短く、"
        "reason は判定の理由を日本語で簡潔に答えてください。"
    )
    return {
        "model": model,
        "input": [
            {"type": "text", "text": prompt},
            {"type": "image", "data": b64, "mime_type": "image/jpeg"},
        ],
        "response_format": {"type": "text", "mime_type": "application/json",
                            "schema": RESULT_SCHEMA},
    }


# ---- 通信 ----

def _redact(msg: str, api_key) -> str:
    return msg.replace(api_key, "***") if api_key else msg


def call_gemini(req: dict, api_key: str, endpoint: str = DEFAULT_ENDPOINT,
                timeout: float = 20.0) -> dict:
    """POST して応答(dict)を返す。失敗は GeminiError(キーはヘッダのみで渡し、メッセージにも出さない)。"""
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json",
               "Api-Revision": API_REVISION}
    r = urllib.request.Request(endpoint, data=json.dumps(req).encode("utf-8"),
                               headers=headers, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as res:
            raw = res.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = ""
        raise GeminiError(_redact(f"HTTP {e.code}: {body}", api_key)) from None
    except (TimeoutError, OSError) as e:  # URLError / socket.timeout も OSError の仲間
        reason = getattr(e, "reason", e)
        raise GeminiError(_redact(f"通信失敗または時間切れ: {type(e).__name__}: {reason}",
                                  api_key)) from None
    try:
        resp = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        head = _redact(raw.decode("utf-8", "replace")[:300], api_key)
        raise GeminiError(f"応答が JSON ではありません: {head}") from None
    if not isinstance(resp, dict):
        raise GeminiError("応答が JSON オブジェクトではありません")
    return resp


# ---- 応答の解釈 ----

def _collect_text(node) -> str:
    """node(文字列・辞書・配列)から text を集めて連結する。"""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_collect_text(n) for n in node)
    if isinstance(node, dict):
        t = node.get("text")
        if isinstance(t, str):
            return t
        for k in ("content", "output", "parts", "outputs"):
            if k in node:
                s = _collect_text(node[k])
                if s:
                    return s
    return ""


def _find_json_text(node):
    """辞書・配列を再帰的にたどり、JSON として解釈できる text 文字列を探す。"""
    if isinstance(node, dict):
        t = node.get("text")
        if isinstance(t, str):
            try:
                if isinstance(json.loads(t), (dict, list)):
                    return t
            except ValueError:
                pass
        for v in node.values():
            found = _find_json_text(v)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_json_text(v)
            if found is not None:
                return found
    return None


def extract_text(resp: dict):
    """応答からテキストを取り出す。形が未確認のため複数形式に対応する。取り出せなければ None。
    (1) output_text (2) outputs[{type:text}] (3) steps[type=model_output] (4) candidates (5) 再帰探索"""
    if not isinstance(resp, dict):
        return None
    t = resp.get("output_text")
    if isinstance(t, str) and t.strip():
        return t
    outs = resp.get("outputs")
    if isinstance(outs, list):
        s = "".join(o["text"] for o in outs
                    if isinstance(o, dict) and o.get("type", "text") == "text"
                    and isinstance(o.get("text"), str))
        if s.strip():
            return s
    steps = resp.get("steps")
    if isinstance(steps, list):
        s = "".join(_collect_text(st) for st in steps
                    if isinstance(st, dict) and st.get("type") == "model_output")
        if s.strip():
            return s
    cands = resp.get("candidates")
    if isinstance(cands, list) and cands and isinstance(cands[0], dict):
        s = _collect_text((cands[0].get("content") or {}).get("parts")
                          if isinstance(cands[0].get("content"), dict) else None)
        if s.strip():
            return s
    return _find_json_text(resp)


def parse_result(resp: dict) -> dict:
    """応答 → {"match","confidence","observed","reason"}。足りない・型が違えば GeminiError。"""
    text = extract_text(resp)
    if text is None:
        raise GeminiError("応答からテキストを取り出せませんでした")
    try:
        obj = json.loads(text)
    except ValueError:
        raise GeminiError(f"応答テキストが JSON ではありません: {text[:200]}") from None
    if not isinstance(obj, dict):
        raise GeminiError("応答 JSON がオブジェクトではありません")
    for k, types in (("match", (bool,)), ("confidence", (int, float)),
                     ("observed", (str,)), ("reason", (str,))):
        v = obj.get(k)
        if k not in obj or not isinstance(v, types) or (k == "confidence" and isinstance(v, bool)):
            raise GeminiError(f"応答の項目 {k!r} がありません、または型が不正です")
    return {"match": obj["match"], "confidence": float(obj["confidence"]),
            "observed": obj["observed"], "reason": obj["reason"]}


# ---- 非同期の照合器 ----

def _load_env_noted() -> None:
    """リポジトリ直下の .env を読む(環境変数が優先)。キーを読んだことだけ stderr に出し、値は出さない。"""
    from env_file import load_env_file
    if "GEMINI_API_KEY" in load_env_file():
        print("GEMINI_API_KEY を .env から読み込みました", file=sys.stderr, flush=True)


class GeminiVerifier:
    """submit() は即座に返り、ワーカースレッド(1本)で照合する。結果は on_result(result) に渡す。
    result = {"box_id","zone","status": match|mismatch|skipped|error,"result","elapsed_ms","error",
              "model","expected"}(model / expected は付加情報)。"""

    def __init__(self, catalog, api_key=None, model=None, endpoint=None, timeout: float = 20.0,
                 on_result=None, max_pending: int = 2):
        self.catalog = catalog or {}
        if api_key is None:  # 明示指定(テスト等)のときは .env を読まない
            _load_env_noted()
        self._api_key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY")
        self.model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        self.endpoint = endpoint or DEFAULT_ENDPOINT
        self.timeout = timeout
        self.on_result = on_result
        self.max_pending = max(1, int(max_pending))
        self._cv = threading.Condition()
        self._pending = deque()
        self._history = []
        self._closed = False
        self._thread = None
        if not self._api_key:
            print("[警告] GEMINI_API_KEY が未設定のため、見た目の照合は行いません(認識は通常どおり動きます)。"
                  "リポジトリ直下の .env に GEMINI_API_KEY=... と書くか、環境変数に設定してください。",
                  file=sys.stderr, flush=True)

    def _emit(self, result):
        self._history.append(result)
        del self._history[:-1000]
        cb = self.on_result
        if cb is not None:
            try:
                cb(result)
            except Exception as e:  # コールバックの失敗でワーカーを止めない
                print(f"[警告] 照合結果の処理で例外: {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)

    def _result(self, box_id, zone, status, result=None, elapsed_ms=0, error=None, expected=None,
                quiet=False):
        r = {"box_id": box_id, "zone": zone, "status": status, "result": result,
             "elapsed_ms": elapsed_ms, "error": error, "model": self.model, "expected": expected}
        if quiet:
            r["quiet"] = True
        return r

    def submit(self, box_id, zone, frame, points):
        entry = self.catalog.get(box_id) if isinstance(self.catalog, dict) else None
        expected = entry.get("expected") if isinstance(entry, dict) else None
        if not expected:
            self._emit(self._result(box_id, zone, "skipped", error="台帳に見た目の記載がありません"))
            return
        if not self._api_key:
            # 起動時に警告済みなので Recognizer 側では1行も出さない(quiet)
            self._emit(self._result(box_id, zone, "skipped", error="APIキー未設定", expected=expected,
                                    quiet=True))
            return
        dropped = []
        with self._cv:
            if self._closed:
                return
            self._pending.append((box_id, zone, frame, points, expected))
            while len(self._pending) > self.max_pending:
                dropped.append(self._pending.popleft())
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="GeminiVerifier", daemon=True)
                self._thread.start()
            self._cv.notify()
        for d in dropped:
            self._emit(self._result(d[0], d[1], "skipped", error="照合待ちが多いため古い要求を破棄",
                                    expected=d[4]))

    def _run(self):
        while True:
            with self._cv:
                while not self._pending and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                box_id, zone, frame, points, expected = self._pending.popleft()
            t0 = time.monotonic()
            try:
                img = crop_around(frame, points)
                req = build_request(img, box_id, expected, self.model)
                resp = call_gemini(req, self._api_key, self.endpoint, self.timeout)
                parsed = parse_result(resp)
                ms = int((time.monotonic() - t0) * 1000)
                self._emit(self._result(box_id, zone, "match" if parsed["match"] else "mismatch",
                                        parsed, ms, None, expected))
            except Exception as e:  # 何があってもワーカーは止めない
                ms = int((time.monotonic() - t0) * 1000)
                msg = _redact(f"{type(e).__name__}: {e}" if not isinstance(e, GeminiError) else str(e),
                              self._api_key)
                self._emit(self._result(box_id, zone, "error", None, ms, msg, expected))

    def results(self):
        return list(self._history)

    def close(self, timeout: float = 3.0):
        """待ち行列を捨ててスレッドを止める。処理中の1件は timeout 秒まで待つ。"""
        with self._cv:
            self._closed = True
            self._pending.clear()
            self._cv.notify_all()
            th = self._thread
        if th is not None:
            th.join(timeout=timeout)


# ---- 単体CLI ----

def _dry_view(req: dict) -> dict:
    v = copy.deepcopy(req)
    for part in v.get("input", []):
        if isinstance(part, dict) and "data" in part:
            part["data"] = f"<base64 {len(part['data'])} bytes>"
    return v


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    _load_env_noted()
    p = argparse.ArgumentParser(description="箱の見た目を Gemini で照合する(単体確認用)")
    p.add_argument("--image", required=True, help="確認する画像ファイル")
    p.add_argument("--box-id", required=True, help="台帳の箱ID(例 B001)")
    p.add_argument("--catalog", default=DEFAULT_CATALOG, help="台帳 JSON")
    p.add_argument("--expected", default=None, help="期待する見た目(台帳より優先)")
    p.add_argument("--model", "--gemini-model", dest="model", default=None,
                   help=f"モデル名(既定: 環境変数 GEMINI_MODEL、無ければ {DEFAULT_MODEL})")
    p.add_argument("--timeout", type=float, default=20.0, help="HTTP タイムアウト(秒)")
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="エンドポイント(テスト用)")
    p.add_argument("--dry-run", action="store_true", help="送信本文を表示するだけで API を呼ばない")
    a = p.parse_args()

    img = cv2.imread(a.image)
    if img is None:
        print(f"[エラー] 画像を読めません: {a.image}", file=sys.stderr)
        return 1
    expected = a.expected
    if expected is None:
        try:
            expected = (load_catalog(a.catalog).get(a.box_id) or {}).get("expected")
        except (OSError, ValueError) as e:
            print(f"[エラー] 台帳を読めません: {e}", file=sys.stderr)
            return 1
    if not expected:
        print(f"[エラー] {a.box_id} の見た目が台帳にありません。--expected で指定してください。",
              file=sys.stderr)
        return 1
    model = a.model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
    req = build_request(crop_around(img, None), a.box_id, expected, model)
    if a.dry_run:
        print(f"POST {a.endpoint}")
        print("headers: x-goog-api-key: <GEMINI_API_KEY>, Content-Type: application/json, "
              f"Api-Revision: {API_REVISION}")
        print(json.dumps(_dry_view(req), ensure_ascii=False, indent=2))
        return 0
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("[エラー] 環境変数 GEMINI_API_KEY が未設定です(--dry-run なら不要)。"
              "リポジトリ直下の .env に GEMINI_API_KEY=... と書くか、環境変数に設定してください。", file=sys.stderr)
        return 2
    t0 = time.monotonic()
    try:
        resp = call_gemini(req, key, a.endpoint, a.timeout)
    except GeminiError as e:
        print(f"[エラー] {e}", file=sys.stderr)
        return 1
    ms = int((time.monotonic() - t0) * 1000)
    if extract_text(resp) is None:
        print("[注意] 応答からテキストを取り出せませんでした。応答の生JSON(先頭2000文字):")
        print(json.dumps(resp, ensure_ascii=False)[:2000])
        return 1
    try:
        res = parse_result(resp)
    except GeminiError as e:
        print(f"[エラー] {e}")
        print("応答の生JSON(先頭2000文字):")
        print(json.dumps(resp, ensure_ascii=False)[:2000])
        return 1
    print(f"判定: {'一致' if res['match'] else '不一致'} (確信度 {res['confidence']:.2f})")
    print(f"見えた特徴: {res['observed']}")
    print(f"理由: {res['reason']}")
    print(f"所要時間: {ms} ms (model={model})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

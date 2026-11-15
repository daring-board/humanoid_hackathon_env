#!/usr/bin/env python3
"""リポジトリ直下の .env を読んで環境変数に入れる(標準ライブラリのみ)。

- `KEY=VALUE` と `export KEY=VALUE` の行を読む。空行と `#` で始まる行は無視。
- すでに環境変数にあるキーは上書きしない(環境変数が優先)。
- 値の前後の空白と、対になった引用符(" と ')は外す。BOM 付き UTF-8 も読める。
- 戻り値は「今回 os.environ に入れたキー名」のリストだけ。値は返さず、表示もしない(APIキーを漏らさないため)。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _parse_value(raw: str) -> str:
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def load_env_file(path=None) -> list[str]:
    """.env を読み、未設定のキーだけ os.environ に入れる。入れたキー名のリストを返す(値は返さない)。"""
    p = Path(path) if path is not None else DEFAULT_ENV_PATH
    try:
        text = p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    loaded: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue  # 壊れた行は無視(内容は出さない)
        key, value = m.group(1), _parse_value(m.group(2))
        if key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded

"""确定性序列化与哈希。

所有进入摘要/签名/导出的内容都必须经过 canonical_json：
键排序、无空白、UTF-8，保证同一逻辑内容永远得到同一字节串。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    """生成规范 JSON 字节串（排序键、紧凑分隔符、UTF-8）。"""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_digest(value: Any) -> str:
    """对任意可 JSON 序列化对象求内容摘要。"""
    return sha256_hex(canonical_json(value))

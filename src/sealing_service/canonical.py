"""规范序列化、分块与 Merkle 摘要工具。

封账内容的唯一权威表示是“规范化 JSON 字节流”：键排序、无空白、UTF-8 编码。
导出、续传、分块校验全部基于同一份字节流，保证重复导出不会改变签发内容。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    """生成对象的规范化 JSON 字节表示（确定性）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """计算字节流的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(data).hexdigest()


def split_chunks(data: bytes, chunk_size: int) -> list[bytes]:
    """按固定大小切分字节流；最后一块可能不足 chunk_size。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须为正整数")
    if not data:
        return []
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]


def chunk_digests(chunks: list[bytes]) -> list[str]:
    """逐块计算 SHA-256 摘要（十六进制）。"""
    return [sha256_hex(chunk) for chunk in chunks]


def merkle_root(leaf_digests_hex: list[str]) -> str:
    """由叶子摘要（十六进制）计算 Merkle 根；奇数节点复制自身配对。"""
    level = [bytes.fromhex(item) for item in leaf_digests_hex]
    if not level:
        return hashlib.sha256(b"").hexdigest()
    while len(level) > 1:
        level = [
            hashlib.sha256(level[i] + (level[i + 1] if i + 1 < len(level) else level[i])).digest()
            for i in range(0, len(level), 2)
        ]
    return level[0].hex()


def merkle_proof(leaf_digests_hex: list[str], index: int) -> list[dict]:
    """为第 index 个叶子生成 Merkle 证明路径。"""
    if not 0 <= index < len(leaf_digests_hex):
        raise IndexError("叶子下标越界")
    level = [bytes.fromhex(item) for item in leaf_digests_hex]
    proof: list[dict] = []
    cursor = index
    while len(level) > 1:
        if cursor % 2 == 0:
            sibling_index, position = cursor + 1, "right"
        else:
            sibling_index, position = cursor - 1, "left"
        sibling = level[sibling_index] if sibling_index < len(level) else level[cursor]
        proof.append({"position": position, "digest": sibling.hex()})
        level = [
            hashlib.sha256(level[i] + (level[i + 1] if i + 1 < len(level) else level[i])).digest()
            for i in range(0, len(level), 2)
        ]
        cursor //= 2
    return proof


def merkle_verify(leaf_digest_hex: str, proof: list[dict], root_hex: str) -> bool:
    """校验叶子摘要与证明路径是否能还原出给定的 Merkle 根。"""
    current = bytes.fromhex(leaf_digest_hex)
    for step in proof:
        sibling = bytes.fromhex(step["digest"])
        if step["position"] == "left":
            current = hashlib.sha256(sibling + current).digest()
        else:
            current = hashlib.sha256(current + sibling).digest()
    return current.hex() == root_hex

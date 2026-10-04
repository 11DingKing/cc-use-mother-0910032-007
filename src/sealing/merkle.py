"""分块摘要（Merkle 树）。

封账载荷按固定大小分块，每块计算叶子哈希，再逐层归并出根哈希。
任意分块可凭兄弟路径独立验证，无需下载完整载荷。
"""
from __future__ import annotations

import hashlib

CHUNK_SIZE = 4096

_LEAF_PREFIX = b"SEAL-LEAF\x00"
_NODE_PREFIX = b"SEAL-NODE\x00"


def split_chunks(payload: bytes, chunk_size: int = CHUNK_SIZE) -> list[bytes]:
    """把载荷切成固定大小的块；空载荷也保证有一块。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须为正数")
    chunks = [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]
    return chunks or [b""]


def leaf_hash(chunk: bytes) -> str:
    return hashlib.sha256(_LEAF_PREFIX + chunk).hexdigest()


def node_hash(left: str, right: str) -> str:
    return hashlib.sha256(
        _NODE_PREFIX + bytes.fromhex(left) + bytes.fromhex(right)
    ).hexdigest()


def build_tree(chunks: list[bytes]) -> dict:
    """自底向上构建 Merkle 树；奇数节点直接晋升上一层。"""
    level = [leaf_hash(chunk) for chunk in chunks]
    levels = [level]
    while len(level) > 1:
        upper = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                upper.append(node_hash(level[i], level[i + 1]))
            else:
                upper.append(level[i])
        level = upper
        levels.append(level)
    return {
        "chunk_hashes": levels[0],
        "root": levels[-1][0],
        "chunk_count": len(chunks),
        "levels": levels,
    }


def proof_for(levels: list[list[str]], index: int) -> list[dict]:
    """生成指定分块的 Merkle 证明（兄弟节点路径）。"""
    if not 0 <= index < len(levels[0]):
        raise IndexError("分块序号越界")
    proof = []
    cursor = index
    for level in levels[:-1]:
        if cursor % 2 == 0:
            sibling = cursor + 1
            if sibling < len(level):
                proof.append({"position": "R", "hash": level[sibling]})
        else:
            proof.append({"position": "L", "hash": level[cursor - 1]})
        cursor //= 2
    return proof


def verify_proof(chunk: bytes, proof: list[dict], root: str) -> bool:
    """用兄弟路径验证某个分块是否属于根哈希对应的载荷。"""
    current = leaf_hash(chunk)
    for step in proof:
        if step["position"] == "L":
            current = node_hash(step["hash"], current)
        else:
            current = node_hash(current, step["hash"])
    return current == root

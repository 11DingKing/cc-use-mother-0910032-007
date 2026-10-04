"""线程安全的封账数据存储，支持可选的 JSON 文件持久化。"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class Store:
    """按集合存放领域记录；transact 内的修改在退出时原子落盘。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "periods": {},
            "snapshots": {},
            "corrections": {},
            "reopen_requests": {},
            "audit": [],
        }
        if self._path and self._path.exists():
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            for key in base:
                if key in loaded:
                    base[key] = loaded[key]
        return base

    @contextmanager
    def transact(self) -> Iterator[dict[str, Any]]:
        """在锁内修改数据，退出时持久化。"""
        with self._lock:
            yield self._data
            self._save()

    @contextmanager
    def read(self) -> Iterator[dict[str, Any]]:
        """在锁内读取数据，不落盘。"""
        with self._lock:
            yield self._data

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

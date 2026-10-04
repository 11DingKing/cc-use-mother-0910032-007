"""存储层：内存字典 + 可选 JSON 文件持久化（原子写）。"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

COLLECTIONS = (
    "snapshots",
    "corrections",
    "reopen_requests",
    "export_sessions",
    "late_materials",
)


class LedgerStore:
    """简单的文档存储。path 为 None 时纯内存运行（测试用）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {name: {} for name in COLLECTIONS}
        self._data["counters"] = {}
        if self._path and self._path.exists():
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            for name in COLLECTIONS:
                self._data[name] = loaded.get(name, {})
            self._data["counters"] = loaded.get("counters", {})

    def next_id(self, prefix: str) -> str:
        """单调递增的确定性编号，如 SNP-0007。"""
        with self._lock:
            counters = self._data["counters"]
            counters[prefix] = counters.get(prefix, 0) + 1
            return f"{prefix}-{counters[prefix]:04d}"

    def get(self, collection: str, key: str) -> dict | None:
        with self._lock:
            return self._data[collection].get(key)

    def put(self, collection: str, key: str, value: dict) -> None:
        with self._lock:
            self._data[collection][key] = value

    def all(self, collection: str) -> list[dict]:
        with self._lock:
            return list(self._data[collection].values())

    def find(self, collection: str, **conditions: Any) -> list[dict]:
        return [
            item
            for item in self.all(collection)
            if all(item.get(field) == expected for field, expected in conditions.items())
        ]

    def flush(self) -> None:
        """持久化到磁盘（临时文件 + 原子替换）。"""
        if not self._path:
            return
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, sort_keys=True, indent=1),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)

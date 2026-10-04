"""启动封账服务：python3 -m sealing [--port 8091] [--store data/ledger.json]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sealing.api import run_server
from sealing.service import LedgerSealingService
from sealing.store import LedgerStore


def main() -> None:
    parser = argparse.ArgumentParser(description="年度核算审计封账服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--store", default=None, help="JSON 持久化路径，缺省为纯内存")
    args = parser.parse_args()
    service = LedgerSealingService(store=LedgerStore(args.store))
    run_server(service, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

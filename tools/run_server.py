"""启动年度核算审计封账 HTTP 服务。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sealing_service.api import create_server
from sealing_service.service import SealingService
from sealing_service.store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="年度核算审计封账服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-file", default=None, help="可选的 JSON 持久化文件路径")
    parser.add_argument("--chunk-size", type=int, default=4096, help="分块摘要的字节大小")
    args = parser.parse_args()

    service = SealingService(Store(args.data_file), chunk_size=args.chunk_size)
    server = create_server(service, args.host, args.port)
    print(f"封账服务已启动: http://{args.host}:{args.port}（分块大小 {args.chunk_size} 字节）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()

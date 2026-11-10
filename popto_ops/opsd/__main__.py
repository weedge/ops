"""python -m popto_ops.opsd —— launchd 常驻（cn.popto.ops.opsd）。"""
from __future__ import annotations

import logging
import signal
import sys

from .. import paths
from .worker import Worker


def main() -> int:
    paths.ensure_data_dirs()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    w = Worker()
    signal.signal(signal.SIGTERM, lambda *_: w.stop.set())
    try:
        w.run_forever()
    except KeyboardInterrupt:
        w.stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())

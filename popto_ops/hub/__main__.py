"""python -m popto_ops.hub —— launchd 常驻（cn.popto.ops.hub）。"""
from __future__ import annotations

import logging
import signal
import sys

from .. import config, paths
from .core import Hub
from .server import serve


def main() -> int:
    paths.ensure_data_dirs()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    rep = config.check_config()
    for w in rep.warnings:
        logging.warning(w)
    if not rep.ok:
        for e in rep.errors:
            logging.error(e)
        logging.error("配置校验未通过，中台不启动。运行 opsctl rules check 查看详情。")
        return 2
    hub = Hub()
    hcfg = hub.cfg.get("hub") or {}
    srv = serve(hub, hcfg.get("host", "127.0.0.1"), int(hcfg.get("port", 8787)), hcfg.get("token"))
    logging.info("中台已启动 http://%s:%s，规则 %d 条，通知出口 %s", hcfg.get("host", "127.0.0.1"),
                 hcfg.get("port", 8787), len(hub.rules), hub.notifier.name)

    def stop(*_):
        logging.info("收到退出信号")
        import threading

        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

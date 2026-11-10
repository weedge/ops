"""中台 HTTP 接口（标准库 http.server）。一期只监听 127.0.0.1；二期挪到内网时配置 hub.token。"""
from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .core import Hub, HubError

log = logging.getLogger("hub")


def make_handler(hub: Hub, token: str | None):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "popto-ops-hub/0.1"

        def log_message(self, fmt, *args):  # noqa: N802
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, code: int, obj) -> None:
            data = json.dumps(obj, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode() or "{}")

        def _auth(self) -> bool:
            if token and self.headers.get("X-Ops-Token") != token:
                self._send(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):  # noqa: N802
            if not self._auth():
                return
            u = urlparse(self.path)
            qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
            try:
                self._send(200, self._get(u.path, qs))
            except HubError as e:
                self._send(e.status, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("GET %s", self.path)
                self._send(500, {"error": str(e)})

        def do_POST(self):  # noqa: N802
            if not self._auth():
                return
            u = urlparse(self.path)
            try:
                body = self._body()
                self._send(200, self._post(u.path, body))
            except HubError as e:
                self._send(e.status, {"error": str(e)})
            except json.JSONDecodeError:
                self._send(400, {"error": "请求体不是合法 JSON"})
            except Exception as e:  # noqa: BLE001
                log.exception("POST %s", self.path)
                self._send(500, {"error": str(e)})

        def _get(self, path: str, qs: dict):
            if path == "/v1/health":
                return {"ok": True}
            if path == "/v1/status":
                return hub.status()
            if path == "/v1/lease":
                agents = qs.get("agents")
                got = hub.lease(qs.get("worker", "unknown"), agents.split(",") if agents else None,
                                min(float(qs.get("wait", 0)), 55.0))
                return {"task": got}
            if path == "/v1/tasks":
                return {"tasks": hub.list_tasks(qs.get("state"), int(qs.get("limit", 50)))}
            m = re.fullmatch(r"/v1/tasks/([\w.-]+)", path)
            if m:
                return hub.task_detail(m.group(1))
            if path == "/v1/cards":
                return {"cards": hub.list_cards(qs.get("status"), int(qs.get("limit", 30)))}
            if path == "/v1/digest":
                return hub.digest(qs["since"], qs.get("until"))
            if path == "/v1/counter":
                return {"value": hub.counter(qs["name"], qs.get("day"))}
            raise HubError("not found", 404)

        def _post(self, path: str, b: dict):
            if path == "/v1/events":
                return hub.ingest(b)
            m = re.fullmatch(r"/v1/tasks/([\w.-]+)/(renew|result|retry|cancel)", path)
            if m:
                tid, act = m.groups()
                if act == "renew":
                    return {"ok": hub.renew(tid, b.get("worker", ""))}
                if act == "result":
                    return hub.submit_result(tid, b)
                if act == "retry":
                    return hub.retry(tid, b.get("agent"))
                return hub.cancel(tid)
            m = re.fullmatch(r"/v1/cards/([\w.-]+)/decide", path)
            if m:
                return hub.ingest({"source": b.get("source", "opsctl"), "type": "decision.made", "subject": m.group(1),
                                   "summary": f"{m.group(1)} → {b.get('choice')}",
                                   "data": {"card": m.group(1), "choice": b.get("choice"), "reason": b.get("reason"),
                                            "by": b.get("by", "opsctl")}})
            m = re.fullmatch(r"/v1/agents/([\w.-]+)/state", path)
            if m:
                if "usage_ratio" in b and "state" not in b:
                    return hub.set_usage(m.group(1), float(b["usage_ratio"]))
                return hub.set_agent_state(m.group(1), b.get("state", "closed"), open_for=b.get("open_for"),
                                           open_until=b.get("open_until"), error=b.get("error"))
            if path == "/v1/notify":
                oid = hub.enqueue_notice(b.get("kind", "message"), b.get("title", ""), b.get("body", ""),
                                         priority=int(b.get("priority", 2)), ref=b.get("ref"), dedup=bool(b.get("dedup")))
                return {"ok": True, "id": oid}
            if path == "/v1/pause":
                return hub.pause(b["key"], b.get("reason", ""))
            if path == "/v1/resume":
                return hub.resume(b["key"])
            if path == "/v1/tick":
                return hub.tick()
            if path == "/v1/rules/reload":
                return hub.reload()
            if path == "/v1/counter":
                hub.add_counter(b["name"], float(b.get("value", 1)))
                return {"ok": True, "value": hub.counter(b["name"])}
            raise HubError("not found", 404)

    return H


def serve(hub: Hub, host: str, port: int, token: str | None = None, tick_seconds: float = 30.0,
          stop: threading.Event | None = None) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(hub, token))
    srv.daemon_threads = True
    stop = stop or threading.Event()

    def ticker():
        while not stop.wait(tick_seconds):
            try:
                hub.tick()
            except Exception:  # noqa: BLE001
                log.exception("tick 失败")

    threading.Thread(target=ticker, name="hub-tick", daemon=True).start()
    return srv

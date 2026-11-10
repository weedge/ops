"""钉钉回调监听（cn.popto.ops.dingtalk-events）：dws event 长连接 → 中台事件。本机不需要公网入口。

支持的回复（只接受 notifier.dingtalk.approvers 里的人）：
  C12 1 [原因]            批卡片 C12，选第 1 个选项（也可写选项 key）
  记下来：……              追加到 decisions.md
  需求：……                转成 demand.manual 进需求池
  已发 pk_xxx <链接>      半自动发布完成后补登记
  暂停 <规则/平台/agent>  /  恢复 <…>
互动卡片按钮回调（user_card_action_triggered）里的 context 带 card_id / choice 时同样生效。
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time

from ..config import load_agents_config
from .. import paths
from ..opsd.client import HubClient

log = logging.getLogger("dingtalk-events")

RE_CARD = re.compile(r"^\s*(?:批\s*)?[Cc](\d+)\s+(\S+)(?:\s+(.+))?\s*$", re.S)
RE_NOTE = re.compile(r"^\s*记下来\s*[:：]\s*(.+)$", re.S)
RE_DEMAND = re.compile(r"^\s*需求\s*[:：]\s*(.+)$", re.S)
RE_PUBLISHED = re.compile(r"^\s*已发\s+(pk_\S+)\s+(\S+)\s*$")
RE_PAUSE = re.compile(r"^\s*(暂停|恢复)\s+(\S+)\s*$")

TEXT_KEYS = ("text", "content", "msgContent", "message")
# 只认 ID，不认昵称（昵称可随意改，能冒充审批人）。dws --flatten 的群消息只带 sender_open_dingtalk_id。
SENDER_KEYS = ("senderId", "sender_id", "senderStaffId", "senderUserId", "userId", "user_id", "operatorUserId",
               "openDingTalkId", "senderOpenDingTalkId", "sender_open_dingtalk_id", "operator_open_dingtalk_id")


def _walk(obj, keys) -> list[str]:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, (str, int)):
                out.append(str(v))
            elif k in keys and isinstance(v, dict) and isinstance(v.get("content"), str):
                out.append(v["content"])
            else:
                out += _walk(v, keys)
    elif isinstance(obj, list):
        for v in obj:
            out += _walk(v, keys)
    elif isinstance(obj, str) and obj.startswith("{"):
        try:
            out += _walk(json.loads(obj), keys)
        except json.JSONDecodeError:
            pass
    return out


def parse_command(text: str) -> dict | None:
    """把一条消息解析成中台事件；不认识的返回 None。"""
    text = (text or "").strip()
    text = re.sub(r"^@\S+\s*", "", text)  # 去掉开头的 @机器人
    if m := RE_CARD.match(text):
        return {"type": "decision.made", "subject": f"C{m.group(1)}", "summary": text[:200],
                "data": {"card": f"C{m.group(1)}", "choice": m.group(2), "reason": (m.group(3) or "").strip() or None}}
    if m := RE_NOTE.match(text):
        return {"type": "decision.note", "summary": m.group(1)[:200], "data": {"text": m.group(1).strip()}}
    if m := RE_DEMAND.match(text):
        return {"type": "demand.manual", "subject": "demand:manual", "summary": m.group(1)[:300],
                "data": {"text": m.group(1).strip()}}
    if m := RE_PUBLISHED.match(text):
        return {"type": "content.published.manual", "subject": f"pack:{m.group(1)}", "summary": text[:200],
                "data": {"pack_id": m.group(1), "url": m.group(2)}}
    if m := RE_PAUSE.match(text):
        return {"type": "_pause" if m.group(1) == "暂停" else "_resume", "key": m.group(2)}
    return None


def handle_event(raw: dict, hub: HubClient, approvers: list[str], allow_any: bool = False) -> dict | None:
    etype = raw.get("event_type") or raw.get("type") or ""
    senders = set(_walk(raw, SENDER_KEYS))
    if not allow_any and not (senders & set(map(str, approvers))):
        log.info("忽略非审批人的消息（发送者 %s）。如需允许，把其 userId 加进 notifier.dingtalk.approvers", senders or "未知")
        return None
    who = sorted(senders & set(map(str, approvers)))[:1] or sorted(senders)[:1] or ["dingtalk"]
    if "card_action" in etype:
        ctx = _walk(raw, ("context",))
        data = {}
        for c in ctx:
            try:
                data = json.loads(c) if isinstance(c, str) else c
                break
            except (json.JSONDecodeError, TypeError):
                continue
        if not data:
            vals = {k: v for k, v in zip(("card_id", "choice"), _walk(raw, ("card_id", "cardId")) + _walk(raw, ("choice", "value")))}
            data = vals
        card = data.get("card_id") or data.get("cardId")
        if card:
            evt = {"type": "decision.made", "subject": card, "summary": f"卡片回调 {card}",
                   "data": {"card": card, "choice": data.get("choice"), "reason": data.get("reason")}}
        else:
            return None
    else:
        texts = _walk(raw, TEXT_KEYS)
        evt = None
        for t in texts:
            evt = parse_command(t)
            if evt:
                break
        if not evt:
            return None
    if evt["type"] == "_pause":
        return hub.post("/v1/pause", {"key": evt["key"], "reason": f"钉钉 {who[0]}"})
    if evt["type"] == "_resume":
        return hub.post("/v1/resume", {"key": evt["key"]})
    evt["source"] = "dingtalk"
    evt.setdefault("data", {})["by"] = who[0]
    mid = raw.get("message_id") or raw.get("messageId") or raw.get("msgId")
    # 事件流和轮询两条路都按消息 ID 去重：同一条回复只处理一次
    evt["dedup_key"] = (f"dingtalk:msg:{mid}" if mid else
                        f"dingtalk:{raw.get('event_id') or raw.get('eventId') or hash(json.dumps(raw, sort_keys=True, default=str))}")
    res = hub.emit(evt)
    if evt["type"] == "decision.made" and isinstance(res, dict) and res.get("ok") is False:
        hub.notify("message", "批红没成功", f"{evt['summary']}\n\n{res.get('error')}", priority=1)
    return res


CONNECT_GRACE = 180  # dws 启动后这么久还没连上就重启它（网络抖动时 dws 可能一直卡在 connecting）
_STATUS: dict[str, dict] = {}
_STATUS_LOCK = threading.Lock()


def _write_status(key: str, **kw) -> None:
    with _STATUS_LOCK:
        _STATUS.setdefault(key, {}).update(kw, updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        try:
            p = paths.ops_data() / "listener.status.json"
            p.write_text(json.dumps(_STATUS, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


def _watch_stderr(proc: subprocess.Popen, state: dict, key: str) -> None:
    """转发 dws 的 stderr，并从 transport 行里读连接状态。"""
    for line in proc.stderr:  # type: ignore[union-attr]
        sys.stderr.write(line)
        low = line.lower()
        m = re.search(r"bus_pid=(\d+)", line)
        if m:
            state["bus_pid"] = int(m.group(1))
        if '"state":"connected"' in low.replace(" ", "") or "state: connected" in low:
            state["connected"] = True
            _write_status(key, connected=True, since=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        elif any(k in low.replace(" ", "") for k in ('"state":"connecting"', '"state":"reconnecting"', '"state":"disconnected"',
                                                     '"state":"degraded"', "state:degraded")):
            state["connected"] = False
            state["changed"] = time.monotonic()
            _write_status(key, connected=False, last_error=line.strip()[:300])


def consume(cmd: list[str], hub: HubClient, approvers: list[str], allow_any: bool, stop: threading.Event) -> None:
    backoff = 5
    key = cmd[3] if len(cmd) > 3 else "dws"
    while not stop.is_set():
        log.info("启动：%s", " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        state = {"connected": False, "changed": time.monotonic()}
        _write_status(key, connected=False, started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        threading.Thread(target=_watch_stderr, args=(proc, state, key), daemon=True).start()

        def _watchdog():
            while proc.poll() is None and not stop.is_set():
                if not state["connected"] and time.monotonic() - state["changed"] > CONNECT_GRACE:
                    # 多个订阅共用一个 dws event _bus 进程；bus 卡在 degraded 时只重启订阅没用，要连 bus 一起重启
                    bus = state.get("bus_pid")
                    log.warning("dws event %s 超过 %ds 没连上，重启订阅%s", key, CONNECT_GRACE,
                                f"和 bus（pid {bus}）" if bus else "")
                    _write_status(key, connected=False, last_error=f"{CONNECT_GRACE}s 未连上，已重启")
                    if bus:
                        try:
                            os.kill(bus, signal.SIGTERM)
                        except OSError:
                            pass
                    proc.terminate()
                    return
                stop.wait(5)
        threading.Thread(target=_watchdog, daemon=True).start()
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    raw = json.loads(line)
                    handle_event(raw, hub, approvers, allow_any)
                    backoff = 5
                except Exception:  # noqa: BLE001
                    log.exception("处理事件失败：%s", line[:300])
                if stop.is_set():
                    break
        finally:
            if proc.poll() is None:
                proc.terminate()  # SIGTERM，让 dws 正常退订；不要 kill -9
                try:
                    proc.wait(15)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if not stop.is_set():
            log.warning("dws event 退出（%s），%ds 后重连", proc.returncode, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 300)


POLL_INTERVAL = 15       # 秒；钉钉个人事件流实测会漏消息，用轮询群消息兜底
POLL_LOOKBACK = 600      # 启动时回看 10 分钟


def poll_group(chat_id: str, hub: HubClient, approvers: list[str], allow_any: bool, stop: threading.Event,
               dws: str = "dws", interval: float = POLL_INTERVAL) -> None:
    from datetime import datetime, timedelta

    state_path = paths.ops_data() / "listener.poll.json"
    try:
        st = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    seen: list[str] = list(st.get("seen") or [])
    since = datetime.now() - timedelta(seconds=POLL_LOOKBACK)
    if st.get("since"):
        since = max(since, datetime.fromisoformat(st["since"]))
    while not stop.is_set():
        started = datetime.now()
        query_from = (since - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")  # 留 1 分钟重叠，靠 seen 去重
        cmd = [dws, "chat", "+messages-list", "--group", chat_id, "--time", query_from, "--limit", "50", "--format", "json"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            data = json.loads(p.stdout or "{}")
            res = data.get("result", data) if isinstance(data, dict) else {}
            if p.returncode != 0 or not isinstance(res, dict) or "messages" not in res:
                raise RuntimeError((p.stderr or p.stdout or "无输出")[-300:])
            for m in sorted(res.get("messages") or [], key=lambda x: str(x.get("createTime"))):
                mid = m.get("messageId")
                if not mid or mid in seen:
                    continue
                seen.append(mid)
                try:
                    handle_event({"type": "group_message_poll", **m}, hub, approvers, allow_any)
                except Exception:  # noqa: BLE001
                    log.exception("处理轮询到的消息失败：%s", str(m)[:300])
            since = started
            seen = seen[-300:]
            try:
                state_path.write_text(json.dumps({"since": since.isoformat(timespec="seconds"), "seen": seen}),
                                      encoding="utf-8")
            except OSError:
                pass
            _write_status("group_poll", connected=True, since=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        except Exception as e:  # noqa: BLE001
            log.warning("轮询群消息失败：%s", str(e)[:300])
            _write_status("group_poll", connected=False, last_error=str(e)[:300])
        stop.wait(interval)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_agents_config()
    h = cfg.get("hub") or {}
    hub = HubClient(f"http://{h.get('host', '127.0.0.1')}:{h.get('port', 8787)}", h.get("token"))
    dt = (cfg.get("notifier") or {}).get("dingtalk") or {}
    approvers = [str(x) for x in dt.get("approvers") or []]
    allow_any = "--allow-any" in sys.argv
    if not approvers and not allow_any:
        log.error("notifier.dingtalk.approvers 为空：为安全起见不接受任何人的批红。请在 local.yaml 配置后重启。")
    stop = threading.Event()
    threads = []
    if dt.get("chat_id"):
        cmd = ["dws", "event", "consume", "user_im_message_receive_group", "--group", dt["chat_id"], "--flatten", "-f", "ndjson"]
        threads.append(threading.Thread(target=consume, args=(cmd, hub, approvers, allow_any, stop), daemon=True))
        threads.append(threading.Thread(target=poll_group, args=(dt["chat_id"], hub, approvers, allow_any, stop), daemon=True))
    else:
        log.warning("未配置 notifier.dingtalk.chat_id，只监听互动卡片回调")
    cmd = ["dws", "event", "consume", "user_card_action_triggered", "--flatten", "-f", "ndjson"]
    threads.append(threading.Thread(target=consume, args=(cmd, hub, approvers, allow_any, stop), daemon=True))
    for t in threads:
        t.start()

    def _term(*_):
        raise KeyboardInterrupt

    import signal

    signal.signal(signal.SIGTERM, _term)
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())

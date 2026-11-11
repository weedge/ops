"""事件中台核心逻辑（不调用大模型，只做规则匹配、去重、排队、留底和通知控制）。

所有对 hub.db 的访问都在 self.lock 内完成；HTTP 层是多线程的，长轮询通过 Condition 等待。
"""
from __future__ import annotations

import fnmatch
import json
import logging
import re
import threading
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from .. import config as cfgmod
from .. import db, governance, paths, scheduler
from ..ids import new_id
from ..notifier import Notifier, build_notifier
from ..notifier.render import render_card, render_card_batch, short_code
from ..timeutil import in_window, iso, now, parse_duration, parse_iso, today_start, window_end

log = logging.getLogger("hub")

ACTIVE = ("leased",)
INTERNAL_TYPES = {"decision.made", "decision.note", "agent.unavailable"}


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def _load(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default


class HubError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


class Hub:
    def __init__(self, home: Path | None = None, hub_db: Path | None = None, notifier: Notifier | None = None):
        self.home = home or paths.ops_home()
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.conn = db.open_hub(hub_db)
        self.cfg: dict = {}
        self.rules: list[dict] = []
        self.schedules: list[dict] = []
        self._load_config()
        self.notifier = notifier or build_notifier(self.cfg)

    # ------------------------------------------------------------------ 配置
    def _load_config(self) -> None:
        self.cfg = cfgmod.load_agents_config(self.home)
        self.rules = cfgmod.load_rules(self.home)
        self.schedules = cfgmod.load_schedules(self.home)
        self.rule_index = {r["name"]: r for r in self.rules}

    def reload(self) -> dict:
        rep = cfgmod.check_config(self.home)
        if rep.ok:
            with self.lock:
                self._load_config()
                self.cond.notify_all()
        return {"ok": rep.ok, "errors": rep.errors, "warnings": rep.warnings}

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return db.rows(self.conn.execute(sql, args))

    def q1(self, sql: str, args: tuple = ()) -> dict | None:
        r = self.q(sql, args)
        return r[0] if r else None

    # ------------------------------------------------------------------ 暂停
    def is_paused(self, *keys: str) -> bool:
        ks = [k for k in keys if k] + ["all"]
        marks = ",".join("?" * len(ks))
        return bool(self.q(f"SELECT 1 FROM paused WHERE key IN ({marks})", tuple(ks)))

    def pause(self, key: str, reason: str = "") -> dict:
        key = self._norm_pause_key(key)
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO paused(key, reason, created_at) VALUES (?,?,?)", (key, reason, iso()))
        return {"paused": key}

    def resume(self, key: str) -> dict:
        key = self._norm_pause_key(key)
        with self.tx() as c:
            n = c.execute("DELETE FROM paused WHERE key=?", (key,)).rowcount
        with self.lock:
            self.cond.notify_all()
        return {"resumed": key, "existed": bool(n)}

    def _norm_pause_key(self, key: str) -> str:
        if key == "all" or ":" in key:
            return key
        if key in self.rule_index:
            return f"rule:{key}"
        if key in (self.cfg.get("agents") or {}):
            return f"agent:{key}"
        return f"source:{key}"

    # ------------------------------------------------------------------ 事件入口
    def match_rule(self, evt: dict) -> dict | None:
        for r in self.rules:
            w = r.get("when") or {}
            src = w.get("source", "*")
            srcs = [src] if isinstance(src, str) else list(src)
            if "*" not in srcs and evt.get("source") not in srcs:
                continue
            if not any(fnmatch.fnmatchcase(evt["type"], t) for t in w.get("types", [])):
                continue
            if w.get("match"):
                text = f"{evt.get('subject') or ''}\n{evt.get('summary') or ''}"
                if not re.search(w["match"], text):
                    continue
            return r
        return None

    def ingest(self, evt: dict) -> dict:
        if not evt.get("source") or not evt.get("type"):
            raise HubError("事件必须包含 source 和 type")
        e = {
            "id": evt.get("id") or new_id("evt"),
            "source": str(evt["source"]),
            "type": str(evt["type"]),
            "subject": evt.get("subject"),
            "summary": (evt.get("summary") or "")[:500],
            "link": evt.get("link"),
            "payload_ref": evt.get("payload_ref"),
            "data": evt.get("data"),
            "dedup_key": evt.get("dedup_key"),
            "occurred_at": evt.get("occurred_at") or iso(),
        }
        with self.lock:
            if e["dedup_key"]:
                old = self.q1("SELECT id, routed FROM events WHERE dedup_key=?", (e["dedup_key"],))
                if old:
                    with self.tx() as c:
                        c.execute("UPDATE events SET seen_count=seen_count+1, last_seen_at=? WHERE id=?",
                                  (iso(), old["id"]))
                    return {"id": old["id"], "duplicate": True, "routed": old["routed"]}
            with self.tx() as c:
                c.execute(
                    "INSERT INTO events(id,source,type,subject,summary,link,payload_ref,data,dedup_key,occurred_at,"
                    "received_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (e["id"], e["source"], e["type"], e["subject"], e["summary"], e["link"], e["payload_ref"],
                     _j(e["data"]) if e["data"] is not None else None, e["dedup_key"], e["occurred_at"], iso(), iso()),
                )
            if e["type"] in INTERNAL_TYPES:
                res = self._handle_internal(e)
                self._mark_event(e["id"], "internal", None)
                return {"id": e["id"], "routed": "internal", **res}
            rule = self.match_rule(e)
            if not rule:
                self._mark_event(e["id"], "unmatched", None)
                return {"id": e["id"], "routed": "unmatched"}
            if rule.get("to") in (None, "none"):
                self._mark_event(e["id"], "none", rule["name"])
                if rule.get("tell") == "always":
                    self.enqueue_notice("alert" if rule.get("priority", 2) == 0 else "message",
                                        f"{rule['name']}：{e['summary'] or e['type']}",
                                        self._event_md(e), priority=rule.get("priority", 2), ref=e["id"])
                return {"id": e["id"], "routed": "none", "rule": rule["name"]}
            task_id = self._route_to_task(e, rule)
            self._mark_event(e["id"], "task" if task_id else "max_rounds", rule["name"])
            self.cond.notify_all()
            return {"id": e["id"], "routed": "task" if task_id else "max_rounds", "rule": rule["name"], "task_id": task_id}

    def _event_md(self, e: dict) -> str:
        lines = [f"**{e['type']}** · {e.get('source')}", "", e.get("summary") or ""]
        if e.get("subject"):
            lines.append(f"\n对象：`{e['subject']}`")
        if e.get("link"):
            lines.append(f"\n链接：{e['link']}")
        return "\n".join(lines)

    def _mark_event(self, eid: str, routed: str, rule: str | None) -> None:
        with self.tx() as c:
            c.execute("UPDATE events SET routed=?, rule=? WHERE id=?", (routed, rule, eid))

    def _group_of(self, rule: dict, e: dict) -> str | None:
        g = rule.get("group")
        if not g:
            return None
        return (g.replace("{subject}", e.get("subject") or "-")
                 .replace("{source}", e.get("source") or "-")
                 .replace("{type}", e.get("type") or "-"))

    def _route_to_task(self, e: dict, rule: dict) -> str | None:
        grp = self._group_of(rule, e)
        data = e.get("data") or {}
        producer = data.get("producer") if isinstance(data, dict) else None
        with self.tx() as c:
            if grp:
                q = c.execute("SELECT id FROM tasks WHERE rule=? AND grp=? AND state='queued' ORDER BY created_at LIMIT 1",
                              (rule["name"], grp)).fetchone()
                if q:  # 防抖 / 运行中追加：并入同组排队中的那一轮
                    c.execute("INSERT OR IGNORE INTO task_events VALUES (?,?)", (q["id"], e["id"]))
                    if producer:
                        c.execute("UPDATE tasks SET producer=COALESCE(producer, ?) WHERE id=?", (producer, q["id"]))
                    return q["id"]
                last = c.execute("SELECT MAX(round) AS r FROM tasks WHERE rule=? AND grp=?", (rule["name"], grp)).fetchone()
                rnd = (last["r"] or 0) + 1
            else:
                rnd = 1
            if rule.get("max_rounds") and rnd > int(rule["max_rounds"]):
                return None
            deb = parse_duration(rule.get("debounce"), timedelta(0))
            tid = new_id("t")
            parent = data.get("parent_task") if isinstance(data, dict) else None
            c.execute(
                "INSERT INTO tasks(id,rule,grp,source,state,priority,round,created_at,updated_at,not_before,producer,parent_task)"
                " VALUES (?,?,?,?, 'queued',?,?,?,?,?,?,?)",
                (tid, rule["name"], grp, e["source"], int(rule.get("priority", 2)), rnd, iso(), iso(),
                 iso(now() + deb), producer, parent),
            )
            c.execute("INSERT INTO task_events VALUES (?,?)", (tid, e["id"]))
            return tid

    # ------------------------------------------------------------------ 内部事件
    def _handle_internal(self, e: dict) -> dict:
        data = e.get("data") or {}
        if e["type"] == "decision.made":
            ref = data.get("card") or e.get("subject") or ""
            if e["source"] == "dingtalk":
                deny = self.approver_denied(ref, str(data.get("by") or ""))
                if deny:
                    return {"ok": False, "error": deny}
            return self.decide(ref, data.get("choice"),
                               by=data.get("by") or e["source"], reason=data.get("reason"))
        if e["type"] == "decision.note":
            text = data.get("text") or e.get("summary") or ""
            if not text.strip():
                return {"ok": False, "error": "空内容"}
            entry = governance.append_decision(text, source=f"{e['source']} {e['id']}", home=self.home)
            return {"ok": True, "entry": entry}
        if e["type"] == "agent.unavailable":
            agent = data.get("agent")
            if agent:
                self.set_agent_state(agent, "open", open_for=data.get("open_for", "10m"),
                                     error=e.get("summary"))
            return {"ok": True}
        return {"ok": False}

    # ------------------------------------------------------------------ Agent 状态 / 熔断
    def agent_states(self) -> dict[str, dict]:
        return {r["agent"]: r for r in self.q("SELECT * FROM agent_state")}

    def set_agent_state(self, agent: str, state: str, *, open_for: str | None = None, open_until: str | None = None,
                        error: str | None = None, usage_ratio: float | None = None) -> dict:
        with self.tx() as c:
            row = c.execute("SELECT * FROM agent_state WHERE agent=?", (agent,)).fetchone()
            if not row:
                c.execute("INSERT INTO agent_state(agent,state,updated_at) VALUES (?, 'closed', ?)", (agent, iso()))
                row = c.execute("SELECT * FROM agent_state WHERE agent=?", (agent,)).fetchone()
            until = open_until
            if state == "open" and not until:
                until = iso(now() + (parse_duration(open_for) or timedelta(minutes=60)))
            since = row["unavailable_since"]
            if state == "open":
                since = since or iso()
            elif state == "closed":
                since, until = None, None
            c.execute(
                "UPDATE agent_state SET state=?, open_until=?, last_error=COALESCE(?, last_error), updated_at=?,"
                " unavailable_since=?, usage_ratio=COALESCE(?, usage_ratio),"
                " timeout_strikes=CASE WHEN ?='closed' THEN 0 ELSE timeout_strikes END WHERE agent=?",
                (state, until, error, iso(), since, usage_ratio, state, agent),
            )
        with self.lock:
            self.cond.notify_all()
        return self.agent_states().get(agent, {})

    def set_usage(self, agent: str, ratio: float) -> dict:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO agent_state(agent,state,updated_at) VALUES (?, 'closed', ?)", (agent, iso()))
            c.execute("UPDATE agent_state SET usage_ratio=?, updated_at=? WHERE agent=?", (ratio, iso(), agent))
        return self.agent_states().get(agent, {})

    def _agent_failed(self, agent: str, kind: str, detail: str | None, retry_at: str | None) -> None:
        cb = self.cfg.get("circuit_breaker") or {}
        if kind == "quota":
            self.set_agent_state(agent, "open", open_until=retry_at, open_for=cb.get("quota_cooldown", "60m"), error=detail)
        elif kind == "auth":
            self.set_agent_state(agent, "open", open_for=cb.get("auth_cooldown", "6h"), error=detail)
            self.enqueue_notice("alert", f"{agent} 登录失效，需要重新登录",
                                f"{agent} 返回认证错误，已熔断。请在终端重新登录（例如 `! {agent}`），完成后运行 `opsctl agents reset {agent}`。\n\n```\n{(detail or '')[:400]}\n```",
                                priority=0, ref=f"auth:{agent}:{today_start().date()}", dedup=True)
        elif kind == "network":
            self.set_agent_state(agent, "open", open_for=cb.get("network_cooldown", "5m"), error=(detail or "")[-300:])
        elif kind == "timeout":
            with self.tx() as c:
                c.execute("INSERT OR IGNORE INTO agent_state(agent,state,updated_at) VALUES (?, 'closed', ?)", (agent, iso()))
                c.execute("UPDATE agent_state SET timeout_strikes=timeout_strikes+1, updated_at=? WHERE agent=?", (iso(), agent))
                strikes = c.execute("SELECT timeout_strikes FROM agent_state WHERE agent=?", (agent,)).fetchone()[0]
            if strikes >= int(cb.get("timeout_strikes", 3)):
                self.set_agent_state(agent, "open", open_for=cb.get("timeout_cooldown", "30m"), error="连续超时")
                with self.tx() as c:
                    c.execute("UPDATE agent_state SET timeout_strikes=0 WHERE agent=?", (agent,))

    def _agent_ok(self, agent: str) -> None:
        st = self.agent_states().get(agent)
        if st and (st["state"] != "closed" or st["timeout_strikes"]):
            self.set_agent_state(agent, "closed")

    def candidate_chain(self, rule: dict, task: dict) -> list[str]:
        chain = cfgmod.rule_chain(rule, self.cfg)
        if task.get("forced_agent"):
            chain = [task["forced_agent"]]
        # 额度策略：Claude 周用量超线时，P2 改由 Codex 主跑，P3 不用 Claude
        pol = (self.cfg.get("usage_policy") or {}).get("claude") or {}
        st = self.agent_states().get("claude") or {}
        ratio = st.get("usage_ratio")
        prio = int(task.get("priority", 2))
        if ratio is not None and "claude" in chain and not task.get("forced_agent"):
            if prio >= 3 and ratio >= pol.get("p3_threshold", 0.5):
                chain = [a for a in chain if a != "claude"]
            elif prio >= 2 and ratio >= pol.get("p2_threshold", 0.7) and "codex" in chain:
                chain = ["codex"] + [a for a in chain if a != "codex"]
        # 生产者与审查者不能相同
        if rule.get("capability") == "review" and task.get("producer"):
            chain = [a for a in chain if a != task["producer"]]
        return chain

    # ------------------------------------------------------------------ 领任务
    def lease(self, worker: str, agents: list[str] | None = None, wait: float = 0, alive=None) -> dict | None:
        deadline = now().timestamp() + max(0.0, wait)
        with self.cond:
            while True:
                if alive is not None and not alive():
                    return None  # 等待期间接单程序已断开：不要把任务派给一个死连接
                got = self._try_lease(worker, agents)
                if got:
                    return got
                remain = deadline - now().timestamp()
                if remain <= 0:
                    return None
                self.cond.wait(timeout=min(remain, 5.0))

    def _try_lease(self, worker: str, agents: list[str] | None) -> dict | None:
        tnow = iso()
        cands = self.q("SELECT * FROM tasks WHERE state='queued' AND (not_before IS NULL OR not_before<=?)"
                       " ORDER BY priority, created_at", (tnow,))
        if not cands:
            return None
        leased = self.q("SELECT rule, grp, agent FROM tasks WHERE state='leased'")
        busy_groups = {r["grp"] for r in leased if r["grp"]}
        rule_running: dict[str, int] = {}
        agent_running: dict[str, int] = {}
        for r in leased:
            rule_running[r["rule"]] = rule_running.get(r["rule"], 0) + 1
            agent_running[r["agent"]] = agent_running.get(r["agent"], 0) + 1
        states = self.agent_states()
        agent_cfg = self.cfg.get("agents") or {}
        for t in cands:
            rule = self.rule_index.get(t["rule"])
            if not rule:
                self._finish(t["id"], "failed", error="规则已删除或停用")
                continue
            if self.is_paused(f"rule:{rule['name']}", f"source:{t['source']}"):
                continue
            if t["grp"] and t["grp"] in busy_groups:
                continue
            if rule_running.get(rule["name"], 0) >= int(rule.get("parallel", 1)):
                continue
            if rule.get("wait_for") and self._upstream_pending(t, rule):
                continue
            chain = self.candidate_chain(rule, t)
            tried = _load(t["tried"], [])
            remaining = [a for a in chain if a not in tried]
            if not remaining:
                self._finish(t["id"], "failed", error=f"降级链已用尽（试过：{', '.join(tried) or '无'}）")
                self._notify_failed(t, rule, tried)
                continue
            pick = None
            # 不可用（熔断、暂停、本机没有）直接顺延；只是忙（并发满）就先排队等：
            # 等满 overflow_after 才允许交给链上下一个，每多等一个 overflow_after 最多再往后顺延一位
            steps = self._overflow_steps(t, rule)
            busy_seen = 0
            for a in remaining:
                if agents is not None and a not in agents:
                    continue
                st = states.get(a)
                if st and st["state"] == "open" and st["open_until"] and st["open_until"] > tnow:
                    continue
                if self.is_paused(f"agent:{a}"):
                    continue
                if agent_running.get(a, 0) >= int((agent_cfg.get(a) or {}).get("concurrency", 1)):
                    busy_seen += 1
                    if busy_seen > steps:
                        break
                    continue
                pick = a
                break
            if not pick:
                continue
            with self.tx() as c:
                n = c.execute(
                    "UPDATE tasks SET state='leased', agent=?, lease_owner=?, lease_until=?, attempts=attempts+1,"
                    " updated_at=? WHERE id=? AND state='queued'",
                    (pick, worker, iso(now() + timedelta(seconds=self._lease_seconds())), tnow, t["id"]),
                ).rowcount
            if not n:
                continue
            return self._lease_payload(t["id"], rule, pick, chain)
        return None

    def _overflow_steps(self, t, rule: dict) -> int:
        """排队等了几个 overflow_after（默认 10 分钟，规则里可单独设；0 表示不等，忙就顺延）。
        从可以派单的时刻算起：进队 / 重新进队（updated_at）和防抖结束（not_before）取较晚的。"""
        raw = rule.get("overflow_after")
        if raw is None:
            raw = (self.cfg.get("hub") or {}).get("overflow_after")
        try:
            wait = parse_duration(raw, timedelta(minutes=10))
        except ValueError:
            wait = timedelta(minutes=10)
        if not wait or wait.total_seconds() <= 0:
            return 1 << 30
        stamps = [s for s in (t["updated_at"] or t["created_at"], t["not_before"]) if s]
        if not stamps:
            return 0
        waited = (now() - max(parse_iso(s) for s in stamps)).total_seconds()
        return max(0, int(waited // wait.total_seconds()))

    def _upstream_pending(self, t, rule: dict) -> bool:
        """wait_for：比本任务先创建的上游任务（如日报依赖的采集）还没跑完就先不派，最多等 wait_max（默认 30m）。"""
        if now() - parse_iso(t["created_at"]) > parse_duration(rule.get("wait_max"), timedelta(minutes=30)):
            return False
        names = list(rule["wait_for"])
        row = self.q1(f"SELECT 1 FROM tasks WHERE state IN ('queued','leased') AND created_at<=? AND id<>?"
                      f" AND rule IN ({','.join('?' * len(names))}) LIMIT 1", (t["created_at"], t["id"], *names))
        return row is not None

    def unlease(self, tid: str, *, reason: str = "") -> None:
        """派出但对方没收到的任务：放回队列，不算一次尝试、不算租约丢失。"""
        with self.tx() as c:
            c.execute("UPDATE tasks SET state='queued', lease_owner=NULL, lease_until=NULL,"
                      " attempts=MAX(attempts-1, 0), agent=NULL, updated_at=? WHERE id=? AND state='leased'", (iso(), tid))
        log.info("任务 %s 放回队列：%s", tid, reason)
        with self.lock:
            self.cond.notify_all()

    def _lease_seconds(self) -> int:
        return int((self.cfg.get("hub") or {}).get("lease_seconds", 120))

    def _lease_payload(self, tid: str, rule: dict, agent: str, chain: list[str]) -> dict:
        task = self.q1("SELECT * FROM tasks WHERE id=?", (tid,))
        events = self.q("SELECT e.* FROM events e JOIN task_events te ON te.event_id=e.id WHERE te.task_id=?"
                        " ORDER BY e.received_at", (tid,))
        for ev in events:
            ev["data"] = _load(ev["data"])
        primary = cfgmod.rule_chain(rule, self.cfg)[:1]
        return {
            "task": task, "rule": rule, "events": events, "agent": agent,
            "is_fallback": bool(primary and agent != primary[0]),
            "chain": chain, "lease_seconds": self._lease_seconds(),
        }

    def renew(self, tid: str, worker: str) -> bool:
        with self.tx() as c:
            n = c.execute("UPDATE tasks SET lease_until=?, updated_at=? WHERE id=? AND state='leased' AND lease_owner=?",
                          (iso(now() + timedelta(seconds=self._lease_seconds())), iso(), tid, worker)).rowcount
        return bool(n)

    # ------------------------------------------------------------------ 回传结果
    def submit_result(self, tid: str, body: dict) -> dict:
        run_id = body.get("run_id")
        if not run_id:
            raise HubError("缺少 run_id")
        with self.lock:
            if self.q1("SELECT 1 FROM runs WHERE id=?", (run_id,)):
                return {"ok": True, "duplicate": True}
            task = self.q1("SELECT * FROM tasks WHERE id=?", (tid,))
            if not task:
                raise HubError("任务不存在", 404)
            agent = body.get("agent") or task["agent"]
            stale = task["state"] != "leased" or (body.get("worker") and task["lease_owner"] != body.get("worker"))
            with self.tx() as c:
                c.execute(
                    "INSERT INTO runs(id,task_id,agent,is_fallback,started_at,finished_at,status,error_kind,error_detail,"
                    "session_id,cost_usd,duration_s,output_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, tid, agent, int(bool(body.get("is_fallback"))), body.get("started_at"), iso(),
                     "stale" if stale else body.get("status"), body.get("error_kind"),
                     (body.get("error_detail") or "")[:4000], body.get("session_id"), body.get("cost_usd"),
                     body.get("duration_s"), body.get("output_path")),
                )
            if stale:
                return {"ok": True, "stale": True}
            rule = self.rule_index.get(task["rule"]) or {}
            if body.get("status") == "ok":
                self._agent_ok(agent)
                res = self._apply_output(task, rule, agent, body.get("output") or {})
            else:
                res = self._apply_failure(task, rule, agent, body)
            self.cond.notify_all()
            return {"ok": True, **res}

    def _apply_output(self, task: dict, rule: dict, agent: str, out: dict) -> dict:
        tid = task["id"]
        summary = str(out.get("summary") or "")[:1000]
        for fe in out.get("follow_up_events") or []:
            if not isinstance(fe, dict) or not fe.get("type"):
                continue
            data = dict(fe.get("data") or {})
            data.setdefault("producer", agent)
            data.setdefault("parent_task", tid)
            self.ingest({
                "source": fe.get("source") or "pipeline", "type": fe["type"],
                "subject": fe.get("subject"), "summary": fe.get("summary"), "link": fe.get("link"),
                "payload_ref": fe.get("payload_ref"), "data": data,
                "dedup_key": fe.get("dedup_key") or f"follow:{tid}:{fe['type']}:{fe.get('subject')}",
            })
        for f in out.get("dogfood_friction") or []:
            if not isinstance(f, dict):
                continue
            feat = f.get("feature") or "未知功能"
            self.ingest({
                "source": "opsd", "type": "ops.dogfood.friction", "subject": f"feature:{feat}",
                "summary": f"{feat}：{f.get('problem', '')}"[:300], "data": {**f, "task": tid},
                "dedup_key": f"friction:{tid}:{feat}:{str(f.get('problem', ''))[:60]}",
            })
        for f in out.get("ops_feedback") or []:  # 运营自动化系统自身的问题，不进 Popto 需求池
            if not isinstance(f, dict) or not f.get("problem"):
                continue
            self.ingest({
                "source": "opsd", "type": "ops.system.feedback", "subject": f"ops:{f.get('area') or '其他'}",
                "summary": f"{f.get('area') or '运营系统'}：{f['problem']}"[:300], "data": {**f, "task": tid},
                "dedup_key": f"opsfb:{f.get('area')}:{str(f['problem'])[:60]}",
            })
        if out.get("credits_used"):
            self.add_counter("popto_credits", float(out["credits_used"]))
        tell = rule.get("tell", "digest")
        prio = int(rule.get("priority", task.get("priority", 2)))
        if out.get("status") == "failed":
            self._finish(tid, "failed", summary=summary, output=out, error=summary or "Agent 报告失败")
            if prio <= 1:
                self.enqueue_notice("message", f"任务失败：{rule.get('name')}", f"{summary}\n\n任务：`{tid}`", priority=prio, ref=tid)
            return {"state": "failed"}
        needs = bool(out.get("needs_decision")) or out.get("status") == "needs_decision"
        card_id = None
        if needs and tell in ("needed", "always"):
            if isinstance(out.get("decision_card"), dict):
                card_id = self.create_card(out["decision_card"], [tid], priority=prio)
            else:
                self.enqueue_notice("message", f"{rule.get('name')}：需要你看一下", summary, priority=prio, ref=tid)
        state = "waiting_decision" if card_id else "done"
        self._finish(tid, state, summary=summary, output=out, card_id=card_id)
        if tell == "always" and not card_id:
            body = out.get("message_markdown") or summary or "(无内容)"
            self.enqueue_notice("message", rule.get("name", "通知"), body, priority=prio, ref=tid)
        return {"state": state, "card_id": card_id}

    def _apply_failure(self, task: dict, rule: dict, agent: str, body: dict) -> dict:
        kind = body.get("error_kind") or "error"
        detail = body.get("error_detail")
        self._agent_failed(agent, kind, detail, body.get("retry_at"))
        tried = _load(task["tried"], [])
        # 额度 / 认证问题：Agent 已熔断，lease 会跳过它；不记为“试过”，这样主力恢复后还能接回来，
        # 没有其他可用 Agent 时任务就排队等待（审查、决策、开发不降级到弱模型）。
        if kind not in ("network", "cancelled", "lease_lost", "quota", "auth") and agent not in tried:
            tried.append(agent)
        remaining = [a for a in self.candidate_chain(rule, task) if a not in tried]
        if remaining:
            with self.tx() as c:
                c.execute("UPDATE tasks SET state='queued', lease_owner=NULL, lease_until=NULL, tried=?, error=?,"
                          " updated_at=? WHERE id=?", (_j(tried), f"{agent}:{kind}", iso(), task["id"]))
            return {"state": "queued", "next": remaining[0]}
        with self.tx() as c:
            c.execute("UPDATE tasks SET tried=? WHERE id=?", (_j(tried), task["id"]))
        self._finish(task["id"], "failed", error=f"{agent}:{kind} {(detail or '')[:300]}")
        self._notify_failed(task, rule, tried)
        return {"state": "failed"}

    def _notify_failed(self, task: dict, rule: dict, tried: list[str]) -> None:
        prio = int(rule.get("priority", 2))
        if prio <= 1:
            self.enqueue_notice("alert" if prio == 0 else "message", f"任务失败：{rule.get('name')}",
                                f"所有可用 Agent 都没能完成（试过：{', '.join(tried) or '无'}）。\n\n任务：`{task['id']}`，"
                                f"可用 `opsctl task {task['id']}` 查看，`opsctl retry {task['id']}` 重跑。",
                                priority=prio, ref=task["id"])

    def _finish(self, tid: str, state: str, *, summary: str | None = None, output: dict | None = None,
                card_id: str | None = None, error: str | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE tasks SET state=?, summary=COALESCE(?, summary), output=COALESCE(?, output),"
                      " card_id=COALESCE(?, card_id), error=COALESCE(?, error), lease_owner=NULL, lease_until=NULL,"
                      " updated_at=? WHERE id=?",
                      (state, summary, _j(output) if output is not None else None, card_id, error, iso(), tid))

    # ------------------------------------------------------------------ 卡片（票拟 → 批红）
    def create_card(self, card: dict, task_ids: list[str], priority: int = 2) -> str:
        card = dict(card)
        opts = card.get("options") or [
            {"key": "approve", "label": "同意"},
            {"key": "reject", "label": "不同意", "require_reason": True},
        ]
        card["options"] = opts
        ttl = parse_duration(self.cfg.get("card_default_ttl", "24h"))
        expires = card.get("expires_at") or iso(now() + ttl)
        with self.tx() as c:
            seq = (c.execute("SELECT MAX(seq) FROM cards").fetchone()[0] or 0) + 1
            cid = card.get("card_id") or f"card_{now().strftime('%Y%m%d')}_{seq:04d}"
            if c.execute("SELECT 1 FROM cards WHERE id=?", (cid,)).fetchone():
                cid = f"{cid}_{seq}"
            card["card_id"] = cid
            c.execute("INSERT INTO cards(id,seq,kind,title,payload,task_ids,priority,created_at,expires_at,status)"
                      " VALUES (?,?,?,?,?,?,?,?,?, 'pending')",
                      (cid, seq, card.get("kind"), card.get("title"), _j(card), _j(task_ids), priority, iso(), expires))
            c.execute("INSERT INTO outbox(kind,priority,title,card_id,created_at,status) VALUES ('card',?,?,?,?, 'pending')",
                      (priority, card.get("title"), cid, iso()))
            # 卡片里的本机文件（周报、PRD 等）随卡片发进群：手机上点不开本机路径
            from ..notifier.render import local_file

            for ln in card.get("links") or []:
                lp = local_file(ln)
                if lp:
                    c.execute("INSERT INTO outbox(kind,priority,title,body,card_id,created_at,status,ref)"
                              " VALUES ('file',?,?,?,?,?, 'pending', ?)", (priority, lp.name, str(lp), cid, iso(), f"cardfile:{cid}"))
        return cid

    def find_card(self, ref: str) -> dict | None:
        ref = str(ref).strip()
        m = re.fullmatch(r"[Cc]?(\d+)", ref)
        if m:
            return self.q1("SELECT * FROM cards WHERE seq=?", (int(m.group(1)),))
        return self.q1("SELECT * FROM cards WHERE id=?", (ref,))

    def card_scopes(self, card: dict) -> set[str]:
        """这张卡片可以用来限定审批人的“类型”：产出它的规则名；由确定性脚本产出时再加上卡片 kind。

        Agent 写的卡片 kind 是模型自己填的，不能拿来做权限判断，所以只认规则名。
        """
        rules = [t["rule"] for tid in _load(card["task_ids"], [])
                 if (t := self.q1("SELECT rule FROM tasks WHERE id=?", (tid,)))]
        scopes = set(rules)
        if rules and card.get("kind") and all((self.rule_index.get(r) or {}).get("to") == "script" for r in rules):
            scopes.add(card["kind"])
        return scopes

    def card_chat(self, card_ref: str | None) -> str | None:
        """这张卡片该发到哪个群（notifier.dingtalk.card_chats 按卡片类型分群）；None = 默认群。"""
        routes = ((self.cfg.get("notifier") or {}).get("dingtalk") or {}).get("card_chats") or {}
        if not routes or not card_ref:
            return None
        card = self.find_card(card_ref)
        if not card:
            return None
        scopes = self.card_scopes(card)
        return next((str(chat) for key, chat in routes.items() if key in scopes and chat), None)

    def _target(self, p: dict) -> str | None:
        """发件队列里一条消息的去向：卡片、卡片附件、批红回执和“批红没成功”的提示都跟着卡片走。"""
        ref = str(p.get("ref") or "")
        if p.get("card_id"):
            return self.card_chat(p["card_id"])
        if ref.startswith(("ack:", "reply:")):
            return self.card_chat(ref.split(":", 1)[1])
        return None

    def approver_denied(self, card_ref: str, by: str) -> str | None:
        """钉钉来的批红：全权审批人（approvers）什么卡片都能批；只在 card_approvers 里的人只能批对应类型。"""
        dt = (self.cfg.get("notifier") or {}).get("dingtalk") or {}
        limited = dt.get("card_approvers") or {}
        if not limited or by in {str(x) for x in dt.get("approvers") or []}:
            return None
        card = self.find_card(card_ref)
        if not card:
            return None  # 交给 decide 报“找不到卡片”
        scopes = self.card_scopes(card)
        if any(by in {str(x) for x in ids or []} for key, ids in limited.items() if key in scopes):
            return None
        return f"你没有这类卡片的审批权限（C{card['seq']}「{card['title']}」），请全权审批人处理。"

    def decide(self, card_ref: str, choice: Any, *, by: str = "human", reason: str | None = None) -> dict:
        with self.lock:
            card = self.find_card(card_ref)
            if not card:
                return {"ok": False, "error": f"找不到卡片 {card_ref}"}
            if card["status"] in ("decided", "expired"):
                return {"ok": True, "already": card["status"], "choice": card["choice"]}
            payload = _load(card["payload"], {})
            opts = payload.get("options") or []
            opt = None
            if choice is not None:
                cs = str(choice).strip()
                opt = next((o for o in opts if o.get("key") == cs), None)
                if opt is None and cs.isdigit() and 1 <= int(cs) <= len(opts):
                    opt = opts[int(cs) - 1]
            expired = by == "timeout"
            if opt is None and not expired:
                return {"ok": False, "error": f"无效选项 {choice!r}，可选：" + "、".join(
                    f"{i}={o.get('key')}" for i, o in enumerate(opts, 1))}
            if opt and opt.get("require_reason") and not (reason and reason.strip()) and not expired:
                return {"ok": False, "error": f"选项「{opt.get('label')}」需要写原因"}
            key = opt.get("key") if opt else "expired"
            status = "expired" if expired else "decided"
            # 部分批准：选项的 emit 与明细一一对应时，「C4 1 3,5」只发第 3、5 条
            emits = list((opt or {}).get("emit") or [])
            picked = None
            m = re.fullmatch(r"\s*#?(\d+(?:\s*[,，、\s]\s*#?\d+)*)\s*", reason or "")
            if m and len(emits) > 1 and len(emits) == len(payload.get("items") or []):
                picked = sorted({int(x) for x in re.findall(r"\d+", m.group(1))})
                bad = [i for i in picked if not 1 <= i <= len(emits)]
                if bad:
                    return {"ok": False, "error": f"明细序号超出范围：{bad}（共 {len(emits)} 条）"}
                emits = [emits[i - 1] for i in picked]
                reason = None
            with self.tx() as c:
                c.execute("UPDATE cards SET status=?, decided_at=?, decided_by=?, choice=?, reason=? WHERE id=?",
                          (status, iso(), by, key,
                           reason if picked is None else "只批明细第 " + "、".join(map(str, picked)) + " 条", card["id"]))
                c.execute("UPDATE outbox SET status='cancelled' WHERE card_id=? AND status='pending'", (card["id"],))
            ctx = {"card_id": card["id"], "choice": key, "reason": reason, "decided_by": by}
            emitted = []
            for e in emits:
                d = dict(e.get("data") or {})
                d.update(ctx)
                r = self.ingest({"source": "dingtalk" if by != "timeout" else "hub", "type": e["type"],
                                 "subject": e.get("subject"), "summary": e.get("summary") or f"{payload.get('title')} · {opt.get('label')}",
                                 "link": e.get("link"), "payload_ref": e.get("payload_ref"), "data": d,
                                 "dedup_key": f"decision:{card['id']}:{e['type']}:{e.get('subject')}"})
                emitted.append(r)
            entry = None
            rec = (opt or {}).get("record")
            if rec == "not_doing" and reason:
                entry = governance.append_not_doing(payload.get("title") or card["id"], reason,
                                                    source=f"卡片 {card['id']}", reply=(opt or {}).get("reply"), home=self.home)
            elif rec == "decision" and reason:
                entry = governance.append_decision(reason, source=f"卡片 {card['id']}", home=self.home)
            label = (opt or {}).get("label") or "超时未处理"
            for tid in _load(card["task_ids"], []):
                t = self.q1("SELECT summary FROM tasks WHERE id=?", (tid,))
                if t:
                    self._finish(tid, "done", summary=f"{t['summary'] or ''} · {'已批' if not expired else '超时'}：{label}")
            # 回执：让人在群里看到批红已生效、接下来会发生什么
            nxt = [r.get("rule") for r in emitted if r.get("routed") == "task" and r.get("rule")]
            head = (f"C{card['seq']} 超时，按「{label}」处理" if expired
                    else f"已收到 C{card['seq']} →「{label}」" + (f"（只批第 {'、'.join(map(str, picked))} 条）" if picked else ""))
            lines = [f"✅ {head}", "", f"卡片：{payload.get('title') or card['id']}"]
            if reason:
                lines.append(f"原因：{reason}")
            if entry:
                lines.append("已记入决策档案")
            counted = {n: nxt.count(n) for n in dict.fromkeys(nxt)}
            nxt_text = "、".join(n + (f" ×{c}" if c > 1 else "") for n, c in counted.items())
            lines.append("接下来：" + (nxt_text + "（自动进行，有需要拍板的会再发卡片）" if nxt else "无后续动作"))
            self.enqueue_notice("message", head, "\n".join(lines), priority=1, ref=f"ack:{card['id']}", dedup=True)
            return {"ok": True, "card": card["id"], "choice": key, "emitted": emitted, "entry": entry}

    # ------------------------------------------------------------------ 通知 outbox
    def enqueue_notice(self, kind: str, title: str, body: str, *, priority: int = 2, ref: str | None = None,
                       dedup: bool = False) -> int | None:
        with self.tx() as c:
            if dedup and ref and c.execute("SELECT 1 FROM outbox WHERE ref=? AND kind=?", (ref, kind)).fetchone():
                return None
            cur = c.execute("INSERT INTO outbox(kind,priority,title,body,created_at,status,ref) VALUES (?,?,?,?,?, 'pending', ?)",
                            (kind, priority, title, body, iso(), ref))
            return cur.lastrowid

    def flush_outbox(self) -> dict:
        tnow = now()
        quiet = self.cfg.get("quiet_hours")
        cap = int(self.cfg.get("max_cards_per_day", 10))
        bot = (self.cfg.get("notifier") or {}).get("bot_name", "兔管家")
        sent = deferred = failed = 0
        with self.lock:
            pending = self.q("SELECT * FROM outbox WHERE status='pending' AND (send_after IS NULL OR send_after<=?)"
                             " ORDER BY priority, id", (iso(tnow),))
            if not pending:
                return {"sent": 0}
            in_quiet = in_window(tnow, quiet)
            cards_today = self.q1("SELECT COUNT(*) AS n FROM outbox WHERE kind IN ('card','card_batch') AND status='sent'"
                                  " AND sent_at>=?", (iso(today_start(tnow)),))["n"]
            wake = window_end(tnow, quiet) if in_quiet else None
            tomorrow = window_end(today_start(tnow) + timedelta(days=1), quiet) if quiet else today_start(tnow) + timedelta(days=1, hours=8)
            # 隔夜 / 超限积压的卡片：≥2 张时合并成一条“待批清单”（只算 1 张）
            # 分群时各群各自合并：不把别的群的卡片合进来
            deferred_cards = [p for p in pending if p["kind"] == "card" and p["ref"] == "deferred"]
            by_chat: dict[str | None, list] = {}
            for p in deferred_cards:
                by_chat.setdefault(self._target(p), []).append(p)
            for chat, backlog in by_chat.items():
                if not (len(backlog) >= 2 and not in_quiet and cards_today < cap):
                    continue
                items = []
                for p in backlog:
                    card = self.q1("SELECT * FROM cards WHERE id=?", (p["card_id"],))
                    if card and card["status"] == "pending":
                        items.append((_load(card["payload"], {}), card["seq"], card["expires_at"]))
                if items:
                    title, body = render_card_batch(items, bot)
                    res = self._send("card_batch", title, body, 1, "batch", chat)
                    ok = res.get("ok")
                    with self.tx() as c:
                        for p in backlog:
                            c.execute("UPDATE outbox SET status=?, sent_at=?, error=? WHERE id=?",
                                      ("sent" if ok else "pending", iso() if ok else None, res.get("error"), p["id"]))
                            if ok:
                                c.execute("UPDATE cards SET status='sent', sent_at=? WHERE id=? AND status='pending'",
                                          (iso(), p["card_id"]))
                        if ok:
                            c.execute("INSERT INTO outbox(kind,priority,title,body,created_at,sent_at,status,ref)"
                                      " VALUES ('card_batch',1,?,?,?,?, 'sent', 'batch')", (title, body, iso(), iso()))
                    if ok:
                        cards_today += 1
                        sent += 1
                    pending = [p for p in pending if p not in backlog]
            for p in pending:
                prio = int(p["priority"] if p["priority"] is not None else 2)
                if in_quiet and prio > 0:
                    with self.tx() as c:
                        c.execute("UPDATE outbox SET send_after=?, ref=CASE WHEN kind='card' THEN 'deferred' ELSE ref END"
                                  " WHERE id=?", (iso(wake), p["id"]))
                    deferred += 1
                    continue
                if p["kind"] == "file":
                    # 卡片附件要等卡片本身发出去之后再发
                    if p["card_id"] and not self.q1("SELECT 1 FROM outbox WHERE card_id=? AND kind IN ('card','card_batch')"
                                                    " AND status='sent'", (p["card_id"],)):
                        card = self.q1("SELECT status FROM cards WHERE id=?", (p["card_id"],))
                        if card and card["status"] in ("pending",):
                            continue
                    res = self._send_file(p["body"] or "", p["title"], prio, p["ref"], self._target(p))
                    with self.tx() as c:
                        if res.get("ok"):
                            c.execute("UPDATE outbox SET status='sent', sent_at=?, error=NULL WHERE id=?", (iso(), p["id"]))
                        else:
                            c.execute("UPDATE outbox SET error=?, send_after=? WHERE id=?",
                                      (res.get("error"), iso(now() + timedelta(minutes=5)), p["id"]))
                    sent += bool(res.get("ok"))
                    failed += not res.get("ok")
                    continue
                if p["kind"] == "card":
                    card = self.q1("SELECT * FROM cards WHERE id=?", (p["card_id"],))
                    if not card or card["status"] != "pending":
                        with self.tx() as c:
                            c.execute("UPDATE outbox SET status='cancelled' WHERE id=?", (p["id"],))
                        continue
                    if cards_today >= cap and prio > 0:
                        with self.tx() as c:
                            c.execute("UPDATE outbox SET send_after=?, ref='deferred' WHERE id=?", (iso(tomorrow), p["id"]))
                        deferred += 1
                        continue
                    title, body = render_card(_load(card["payload"], {}), card["seq"], card["expires_at"], bot)
                else:
                    title, body = p["title"] or "", p["body"] or ""
                res = self._send(p["kind"], title, body, prio, p["ref"], self._target(p))
                with self.tx() as c:
                    if res.get("ok"):
                        c.execute("UPDATE outbox SET status='sent', sent_at=?, error=NULL WHERE id=?", (iso(), p["id"]))
                        if p["kind"] == "card":
                            c.execute("UPDATE cards SET status='sent', sent_at=? WHERE id=?", (iso(), p["card_id"]))
                    else:
                        c.execute("UPDATE outbox SET error=?, send_after=? WHERE id=?",
                                  (res.get("error"), iso(now() + timedelta(minutes=5)), p["id"]))
                if res.get("ok"):
                    sent += 1
                    if p["kind"] == "card":
                        cards_today += 1
                else:
                    failed += 1
        return {"sent": sent, "deferred": deferred, "failed": failed}

    def _target_kw(self, target: str | None) -> dict:
        return {"target": target} if target and getattr(self.notifier, "supports_target", False) else {}

    def _send_file(self, path: str, title: str | None, prio: int, ref: str | None, target: str | None = None) -> dict:
        try:
            return self.notifier.send_file(Path(path), title=title, priority=prio, ref=ref, **self._target_kw(target))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:500]}

    def _send(self, kind: str, title: str, body: str, prio: int, ref: str | None, target: str | None = None) -> dict:
        try:
            return self.notifier.send(kind, title, body, priority=prio, ref=ref, **self._target_kw(target))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:500]}

    # ------------------------------------------------------------------ 定时 / 维护
    def tick(self) -> dict:
        fired = self._fire_schedules()
        expired_leases = self._expire_leases()
        expired_cards = self._expire_cards()
        self._alert_unavailable()
        with self.lock:
            self.cond.notify_all()
        out = self.flush_outbox()
        return {"fired": fired, "expired_leases": expired_leases, "expired_cards": expired_cards, "outbox": out}

    def _fire_schedules(self) -> list[str]:
        fired = []
        tnow = now()
        for s in self.schedules:
            typ = s["type"]
            due = scheduler.last_due(s, tnow)
            st = self.q1("SELECT * FROM schedule_state WHERE type=?", (typ,))
            if not st:  # 第一次见到：只记下，不补发（避免一启动就把今天的定时任务全跑一遍）
                with self.tx() as c:
                    c.execute("INSERT INTO schedule_state(type,last_due,fired_at) VALUES (?,?,NULL)", (typ, iso(due)))
                continue
            if st["last_due"] and parse_iso(st["last_due"]) >= due:
                continue
            with self.tx() as c:
                c.execute("UPDATE schedule_state SET last_due=?, fired_at=? WHERE type=?", (iso(due), iso(), typ))
            if tnow - due > scheduler.max_late(s):
                continue
            if s.get("requires") and not self._cfg_path(s["requires"]):
                continue  # 依赖的配置没填（如 ci.projects），不触发，避免空跑刷屏
            late = int((tnow - due).total_seconds() // 60)
            self.ingest({"source": "cron", "type": typ, "subject": f"cron:{due.strftime('%Y-%m-%dT%H:%M')}",
                         "summary": f"定时触发 {typ}" + (f"（补发，晚了 {late} 分钟）" if late > 2 else ""),
                         "dedup_key": f"cron:{typ}:{iso(due)}"})
            fired.append(typ)
        return fired

    def _cfg_path(self, dotted: str):
        cur = self.cfg
        for part in dotted.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur

    def _expire_leases(self) -> int:
        tnow = iso()
        maxl = int((self.cfg.get("hub") or {}).get("max_lease_losses", 3))
        rows = self.q("SELECT * FROM tasks WHERE state='leased' AND lease_until<?", (tnow,))
        for t in rows:
            if t["lease_losses"] + 1 > maxl:
                self._finish(t["id"], "failed", error="多次丢失租约（机器睡眠或进程崩溃）")
            else:
                with self.tx() as c:
                    c.execute("UPDATE tasks SET state='queued', lease_owner=NULL, lease_until=NULL,"
                              " lease_losses=lease_losses+1, updated_at=? WHERE id=? AND state='leased'", (tnow, t["id"]))
        return len(rows)

    def _expire_cards(self) -> int:
        rows = self.q("SELECT * FROM cards WHERE status IN ('pending','sent') AND expires_at<?", (iso(),))
        for card in rows:
            payload = _load(card["payload"], {})
            on_exp = payload.get("on_expire")
            self.decide(card["id"], on_exp, by="timeout", reason="超时未处理，按默认安全选项处理")
        return len(rows)

    def _alert_unavailable(self) -> None:
        limit = parse_duration(self.cfg.get("alert_after_unavailable", "2h"))
        for a, st in self.agent_states().items():
            since = st.get("unavailable_since")
            if since and now() - parse_iso(since) >= limit:
                self.enqueue_notice("alert", f"{a} 已连续 {int(limit.total_seconds() // 3600)} 小时不可用",
                                    f"最近错误：{(st.get('last_error') or '')[:300]}\n\n`opsctl agents` 查看，恢复后 `opsctl agents reset {a}`。",
                                    priority=0, ref=f"unavail:{a}:{since}", dedup=True)

    # ------------------------------------------------------------------ 计数 / 查询
    def add_counter(self, name: str, v: float) -> None:
        day = now().date().isoformat()
        with self.tx() as c:
            c.execute("INSERT INTO counters(day,name,value) VALUES (?,?,?) ON CONFLICT(day,name)"
                      " DO UPDATE SET value=value+excluded.value", (day, name, v))

    def counter(self, name: str, day: str | None = None) -> float:
        r = self.q1("SELECT value FROM counters WHERE day=? AND name=?", (day or now().date().isoformat(), name))
        return float(r["value"]) if r else 0.0

    def retry(self, tid: str, agent: str | None = None) -> dict:
        t = self.q1("SELECT * FROM tasks WHERE id=?", (tid,))
        if not t:
            raise HubError("任务不存在", 404)
        if t["state"] == "leased":
            raise HubError("任务正在运行")
        with self.tx() as c:
            c.execute("UPDATE tasks SET state='queued', tried='[]', forced_agent=?, not_before=?, error=NULL,"
                      " lease_losses=0, updated_at=? WHERE id=?", (agent, iso(), iso(), tid))
        with self.lock:
            self.cond.notify_all()
        return {"ok": True, "task": tid, "agent": agent}

    def cancel(self, tid: str) -> dict:
        self._finish(tid, "cancelled")
        return {"ok": True}

    def task_detail(self, tid: str) -> dict:
        t = self.q1("SELECT * FROM tasks WHERE id=?", (tid,))
        if not t:
            raise HubError("任务不存在", 404)
        t["output"] = _load(t["output"])
        t["tried"] = _load(t["tried"], [])
        return {
            "task": t,
            "events": self.q("SELECT e.* FROM events e JOIN task_events te ON te.event_id=e.id WHERE te.task_id=?", (tid,)),
            "runs": self.q("SELECT * FROM runs WHERE task_id=? ORDER BY finished_at", (tid,)),
            "card": self.q1("SELECT * FROM cards WHERE id=?", (t["card_id"],)) if t["card_id"] else None,
            "run_dir": str(paths.ops_data() / "runs" / tid),
        }

    def list_tasks(self, state: str | None = None, limit: int = 50) -> list[dict]:
        if state:
            return self.q("SELECT id,rule,grp,state,priority,round,agent,attempts,summary,error,created_at,updated_at"
                          " FROM tasks WHERE state=? ORDER BY updated_at DESC LIMIT ?", (state, limit))
        return self.q("SELECT id,rule,grp,state,priority,round,agent,attempts,summary,error,created_at,updated_at"
                      " FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,))

    def list_cards(self, status: str | None = None, limit: int = 30) -> list[dict]:
        sql = "SELECT id,seq,kind,title,status,priority,created_at,sent_at,expires_at,decided_at,decided_by,choice,reason FROM cards"
        if status:
            return self.q(sql + " WHERE status=? ORDER BY seq DESC LIMIT ?", (status, limit))
        return self.q(sql + " ORDER BY seq DESC LIMIT ?", (limit,))

    def status(self) -> dict:
        counts = {r["state"]: r["n"] for r in self.q("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state")}
        return {
            "now": iso(),
            "tasks": counts,
            "agents": self.agent_states(),
            "paused": self.q("SELECT * FROM paused"),
            "cards_pending": self.q1("SELECT COUNT(*) AS n FROM cards WHERE status IN ('pending','sent')")["n"],
            "outbox_pending": self.q1("SELECT COUNT(*) AS n FROM outbox WHERE status='pending'")["n"],
            "credits_today": self.counter("popto_credits"),
            "rules": len(self.rules),
            "notifier": self.notifier.name,
        }

    def digest(self, since: str, until: str | None = None) -> dict:
        until = until or iso()
        tasks = self.q("SELECT id,rule,state,agent,summary,error,updated_at FROM tasks WHERE updated_at>=? AND updated_at<=?"
                       " ORDER BY rule, updated_at", (since, until))
        by_rule: dict[str, list] = {}
        for t in tasks:
            by_rule.setdefault(t["rule"], []).append(t)
        runs = self.q("SELECT r.task_id, r.agent, r.status, r.error_kind, r.is_fallback, t.rule FROM runs r"
                      " JOIN tasks t ON t.id=r.task_id WHERE r.finished_at>=? AND r.finished_at<=?", (since, until))
        fallbacks = [r for r in runs if r["is_fallback"] and r["status"] == "ok"]
        failures = [r for r in runs if r["status"] not in ("ok", "stale")]
        agent_use: dict[str, dict] = {}
        for r in runs:
            a = agent_use.setdefault(r["agent"], {"runs": 0, "ok": 0})
            a["runs"] += 1
            a["ok"] += r["status"] == "ok"
        notes = self.q("SELECT type, COUNT(*) AS n FROM events WHERE routed='none' AND received_at>=? AND received_at<=?"
                       " GROUP BY type", (since, until))
        note_items = self.q("SELECT type, subject, summary, received_at FROM events WHERE routed='none'"
                            " AND received_at>=? AND received_at<=? AND type NOT IN ('social.metrics.snapshot')"
                            " ORDER BY received_at DESC LIMIT 40", (since, until))
        cards = self.q("SELECT seq, title, status, choice, decided_by FROM cards WHERE created_at>=? AND created_at<=?",
                       (since, until))
        return {
            "since": since, "until": until,
            "tasks_by_rule": {k: [{"id": t["id"], "state": t["state"], "agent": t["agent"],
                                   "summary": t["summary"], "error": t["error"]} for t in v] for k, v in by_rule.items()},
            "fallback_runs": fallbacks, "failed_runs": failures, "agent_usage": agent_use,
            "record_only_events": notes, "record_only_items": note_items, "cards": cards,
            "pending_cards": self.list_cards("sent") + self.list_cards("pending"),
            "agents": self.agent_states(),
            "unread_feedback": self.q1("SELECT COUNT(*) AS n FROM events WHERE type='popto.feedback.new'"
                                       " AND received_at>=? AND received_at<=?", (since, until))["n"],
        }

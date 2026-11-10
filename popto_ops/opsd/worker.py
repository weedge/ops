"""接单程序 opsd：长轮询中台领任务 → 准备运行目录 → 拉起 Agent → 校验输出 → 回传（02 §6）。"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from .. import agent_browser
from .. import config as cfgmod
from .. import paths
from ..timeutil import iso, now, parse_duration
from ..validate import build_schema, validate
from . import breaker, video_prep
from .agents import RunResult, RunSpec, adapter_for
from .client import HubClient
from .context import ContextBuilder, build_input_md

log = logging.getLogger("opsd")


def agent_env(cfg: dict) -> dict:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(os.path.expanduser(p) for p in str(cfg.get("path", "")).split(":") if p) or env.get("PATH", "")
    env["BROWSER"] = "/bin/false"
    proxy = cfg.get("proxy")
    if proxy:
        for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            env[k] = proxy
        env["NO_PROXY"] = env["no_proxy"] = cfg.get("no_proxy", "localhost,127.0.0.1")
    # 密钥不进 Agent 环境变量
    for k in list(env):
        if any(s in k.upper() for s in ("TOKEN", "SECRET", "API_KEY", "PASSWORD")) and not k.startswith("OPS_"):
            env.pop(k, None)
    return env


class Worker:
    def __init__(self, cfg: dict | None = None, client: HubClient | None = None, home: Path | None = None):
        self.home = home or paths.ops_home()
        self.cfg = cfg or cfgmod.load_agents_config(self.home)
        h = self.cfg.get("hub") or {}
        self.client = client or HubClient(f"http://{h.get('host', '127.0.0.1')}:{h.get('port', 8787)}", h.get("token"))
        w = self.cfg.get("worker") or {}
        self.name = w.get("name", socket.gethostname())
        self.max_parallel = int(w.get("max_parallel", 6))
        self.renew_seconds = float(w.get("renew_seconds", 30))
        self.active: dict[str, threading.Thread] = {}
        self.stop = threading.Event()
        self.network_ok = True
        self._last_probe = 0.0
        self._browser_lock = threading.Lock()  # 专用浏览器的登录态目录同一时间只能开一个 Chrome

    # ---------------------------------------------------------------- 主循环
    def local_agents(self) -> list[str]:
        out = []
        for name, c in (self.cfg.get("agents") or {}).items():
            try:
                if adapter_for(c).available(c):
                    out.append(name)
            except KeyError:
                log.warning("Agent %s 的 type 未知", name)
        return out

    def probe_proxy(self) -> bool:
        proxy = self.cfg.get("proxy")
        if not proxy:
            return True
        u = urlparse(proxy)
        try:
            with socket.create_connection((u.hostname, u.port or 80), timeout=5):
                return True
        except OSError:
            return False

    def _check_network(self) -> None:
        w = self.cfg.get("worker") or {}
        interval = parse_duration(w.get("proxy_probe_interval" if self.network_ok else "proxy_retry_interval", "10m"))
        if time.monotonic() - self._last_probe < interval.total_seconds():
            return
        self._last_probe = time.monotonic()
        ok = self.probe_proxy()
        if ok != self.network_ok:
            log.warning("内网代理%s", "恢复，继续领任务" if ok else "不通，暂停领任务")
            if not ok:
                try:
                    self.client.notify("alert", "内网代理不通，opsd 暂停领任务",
                                       "机器可能离开了公司网络。任务会在队列里等待，代理恢复后自动继续。",
                                       priority=1, ref=f"proxy-down:{now().strftime('%Y%m%d')}", dedup=True)
                except Exception:  # noqa: BLE001
                    pass
        self.network_ok = ok

    def run_forever(self) -> None:
        agents = self.local_agents()
        log.info("opsd %s 启动，本机可用 Agent：%s", self.name, ", ".join(agents))
        wait = int((self.cfg.get("hub") or {}).get("long_poll_seconds", 50))
        while not self.stop.is_set():
            self._check_network()
            self.active = {k: t for k, t in self.active.items() if t.is_alive()}
            if not self.network_ok:
                # 代理不通时只跑不需要网络的确定性脚本
                avail = [a for a in agents if (self.cfg["agents"][a].get("type") in ("script",))]
            else:
                avail = agents
            if len(self.active) >= self.max_parallel or not avail:
                self.stop.wait(2)
                continue
            try:
                lease = self.client.lease(self.name, avail, wait)
            except Exception as e:  # noqa: BLE001
                log.warning("连不上中台：%s", e)
                self.stop.wait(5)
                continue
            if not lease:
                continue
            tid = lease["task"]["id"]
            t = threading.Thread(target=self.execute_safe, args=(lease,), name=f"task-{tid}", daemon=True)
            self.active[tid] = t
            t.start()

    def execute_safe(self, lease: dict) -> None:
        try:
            self.execute(lease)
        except Exception as e:  # noqa: BLE001
            log.exception("执行任务 %s 出错", lease["task"]["id"])
            try:
                t = lease["task"]
                self.client.result(t["id"], {"run_id": f"r_{t['id']}_{t['attempts']}_{lease['agent']}", "worker": self.name,
                                             "agent": lease["agent"], "status": "error", "error_kind": "error",
                                             "error_detail": f"opsd 内部错误：{e}", "is_fallback": lease.get("is_fallback")})
            except Exception:  # noqa: BLE001
                log.exception("回传失败")

    # ---------------------------------------------------------------- 执行一个任务
    def execute(self, lease: dict) -> dict:
        t, rule, agent = lease["task"], lease["rule"], lease["agent"]
        tid = t["id"]
        acfg = (self.cfg.get("agents") or {})[agent]
        run_dir = paths.ops_data() / "runs" / tid
        run_dir.mkdir(parents=True, exist_ok=True)
        run_id = f"r_{tid}_{t['attempts']}_{agent}"
        (run_dir / "task.json").write_text(json.dumps(lease, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        started = iso()
        t0 = time.monotonic()

        lost = threading.Event()
        done = threading.Event()

        def renewer():
            while not done.wait(self.renew_seconds):
                try:
                    if not self.client.renew(tid, self.name):
                        lost.set()
                        return
                except Exception:  # noqa: BLE001
                    pass  # 中台短暂不可用：租约 2 分钟内恢复即可

        threading.Thread(target=renewer, daemon=True).start()
        try:
            result, out_path = self._run_agent(lease, agent, acfg, run_dir, alive=lambda: not lost.is_set())
        finally:
            done.set()
        body = {"run_id": run_id, "worker": self.name, "agent": agent, "is_fallback": lease.get("is_fallback"),
                "started_at": started, "duration_s": round(time.monotonic() - t0, 1),
                "session_id": result.session_id, "cost_usd": result.cost_usd,
                "output_path": str(out_path) if out_path else None}
        if result.ok and result.output is not None:
            body.update(status="ok", output=result.output)
        else:
            cb = self.cfg.get("circuit_breaker") or {}
            if result.lease_lost:
                kind = "lease_lost"
            elif result.extra.get("invalid"):
                kind = "invalid_output"
            else:
                kind = breaker.classify(result.error_text, timed_out=result.timed_out, cb=cb)
            body.update(status="error", error_kind=kind, error_detail=result.error_text[-3000:],
                        retry_at=breaker.parse_retry_at(result.error_text))
            if kind == "network":
                self._last_probe = 0  # 立刻重探代理
        resp = self.client.result(tid, body)
        log.info("任务 %s [%s] %s → %s %s", tid, rule.get("name"), agent, body["status"], body.get("error_kind") or "")
        return resp

    def _uses_browser(self, rule: dict, acfg: dict) -> bool:
        return (rule.get("tools") == "browser" and acfg.get("type") not in ("script", "inbox") and rule.get("to") != "script"
                and agent_browser.settings(self.cfg)["enabled"])

    def _run_agent(self, lease: dict, agent: str, acfg: dict, run_dir: Path, alive) -> tuple[RunResult, Path | None]:
        if not self._uses_browser(lease["rule"], acfg):
            return self._run_agent_impl(lease, agent, acfg, run_dir, alive)
        benv = {**os.environ, "OPS_RUN_DIR": str(run_dir)}
        with self._browser_lock:  # 等锁期间租约照常续
            try:
                err = agent_browser.open_session(self.cfg, benv)
                if err:
                    return RunResult(ok=True, output={"status": "failed", "needs_decision": False,
                                                      "summary": f"专用浏览器没开起来，任务未执行：{err}"[:600]}), None
                return self._run_agent_impl(lease, agent, acfg, run_dir, alive)
            finally:
                agent_browser.close(self.cfg, benv)

    def _run_agent_impl(self, lease: dict, agent: str, acfg: dict, run_dir: Path, alive) -> tuple[RunResult, Path | None]:
        t, rule = lease["task"], lease["rule"]
        adapter = adapter_for(acfg)
        env = agent_env(self.cfg)
        env.update({"OPS_TASK_FILE": str(run_dir / "task.json"), "OPS_RUN_DIR": str(run_dir),
                    "OPS_HUB_URL": self.client.base, "OPS_DATA": str(paths.ops_data()), "OPS_HOME": str(self.home)})
        if self._uses_browser(rule, acfg):
            env["OPS_BROWSER_PREOPENED"] = "1"
        timeout = parse_duration(rule.get("timeout"), None)
        timeout_s = timeout.total_seconds() if timeout else 900.0
        if acfg.get("type") == "inbox":
            timeout_s = parse_duration(acfg.get("max_wait", "6h")).total_seconds()
        log_path = run_dir / f"attempt-{t['attempts']}-{agent}.log"

        if acfg.get("type") == "script" or rule.get("to") == "script":
            spec = RunSpec(task_id=t["id"], run_dir=run_dir, cwd=self.home, prompt="", schema={}, schema_path=run_dir / "schema.json",
                           tools="workspace", effort=None, model=None, timeout=timeout_s, agent_cfg=acfg, env=env,
                           alive=alive, command=rule.get("command"))
            res = adapter_for({"type": "script"}).run(spec, log_path)
            schema = build_schema(None)
            return self._finalize(res, schema, run_dir)

        skill_dir = cfgmod.find_skill(rule["skill"], self.cfg, self.home)
        if not skill_dir:
            return RunResult(ok=False, error_text=f"找不到 Skill {rule['skill']}"), None
        schema = build_schema(skill_dir if (skill_dir / "schema.json").exists() else None)
        schema_path = run_dir / "schema.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=1), encoding="utf-8")
        (run_dir / "output.json").unlink(missing_ok=True)

        # 积分预算（03 §3.3）：超出单日上限就不跑，按失败进日报
        per_run = rule.get("credits_per_run")
        if per_run:
            budget = float((self.cfg.get("budgets") or {}).get("popto_credits", 0) or 0)
            used = float(self.client.get("/v1/counter", name="popto_credits").get("value", 0))
            if budget and used + float(per_run) > budget:
                return RunResult(ok=True, output={"status": "failed", "needs_decision": False,
                                                  "summary": f"今日 Popto 积分预算已用 {used:.0f}/{budget:.0f}，本任务（约 {per_run} 点）未执行"}), None

        ctx = ContextBuilder(self.client, run_dir)
        notes: list[str] = []
        try:
            ctx.build(list(rule.get("context") or []) + (["event_media"] if rule.get("capability") == "video" and "event_media" not in (rule.get("context") or []) else []), lease)
            if rule.get("capability") == "video" and acfg.get("type") != "antigravity":
                notes += video_prep.prepare(run_dir)
                ctx.files.append(("context/", "降级预处理产物（抽帧宫格、转写）"))
            if self._uses_browser(rule, acfg):
                notes.append(agent_browser.agent_note(self.cfg))
            if lease.get("is_fallback"):
                notes.append(f"你是降级接手的 Agent（主 Agent 不可用）。")
            skill_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            structured = bool(acfg.get("structured_output", acfg.get("type") == "claude"))
            cwd = Path(os.path.expanduser(rule.get("cwd") or str(self.home)))
            if rule.get("tools", "readonly") == "readonly":
                cwd = run_dir
            extra_dirs: list[Path] = []
            if rule.get("worktree"):
                from ..ci import worktree as wtmod

                try:
                    wt = wtmod.prepare(self.cfg, t["id"], lease.get("events") or [])
                except Exception as e:  # noqa: BLE001
                    return RunResult(ok=True, output={"status": "failed", "needs_decision": False, "size": "S",
                                                      "summary": f"开发环境没准备好：{str(e)[:400]}"}), None
                cwd = wt.path
                extra_dirs.append(wtmod.git_common_dir(wt))
                (run_dir / "context").mkdir(exist_ok=True)
                (run_dir / "context" / "worktree.json").write_text(json.dumps(wt.as_dict(), ensure_ascii=False, indent=1))
                notes.append(
                    f"开发环境已由 opsd 准备好：当前目录就是 `{wt.project}` 的独立 worktree（{wt.path}），"
                    f"分支 `{wt.branch}`，基于 `origin/{wt.base}`。直接在这里改代码、跑检查、`git commit`（可多次提交）。"
                    "当前目录已经是 worktree，直接运行 `git status` / `git add` / `git commit`；**不要写 `cd <路径> && …` 这种复合命令**"
                    "（会被权限闸门拦下），需要指定目录时用 `git -C <路径>`、`npm --prefix <路径>`。"
                    "**不要 push、不要开 MR、不要切分支、不要建别的 worktree**：你结束后 opsd 会推送并开 MR，"
                    "MR 标题和描述取你输出的 `mr_title` / `mr_description`。只写 PRD、不改代码时就不要提交。"
                    + ("".join(f"{n}（可以直接跑 lint / 构建 / 测试）。" for n in wt.notes) if wt.notes else
                       "worktree 里没有依赖目录；需要跑检查时在 worktree 里自己安装，不要改主仓库。"))
            errs: list[str] | None = None
            res = RunResult(ok=False)
            for attempt in range(2):  # 输出不合格：带着校验错误同一 Agent 重试 1 次
                prompt = build_input_md(lease, skill_name=rule["skill"], skill_text=skill_text, schema=schema, ctx=ctx,
                                        agent=agent, structured=structured, run_dir=run_dir, extra_notes=notes,
                                        validation_errors=errs)
                (run_dir / "input.md").write_text(prompt, encoding="utf-8")
                spec = RunSpec(task_id=t["id"], run_dir=run_dir, cwd=cwd, prompt=prompt, schema=schema,
                               schema_path=schema_path, tools=rule.get("tools", "readonly"), effort=rule.get("effort"),
                               model=rule.get("model"), timeout=timeout_s, agent_cfg=acfg, env=env, alive=alive,
                               extra_dirs=extra_dirs)
                lp = log_path if attempt == 0 else log_path.with_name(log_path.stem + "-retry.log")
                res = adapter_for(acfg).run(spec, lp)
                if not res.ok:
                    return res, None
                errs = validate(res.output, schema) if res.output is not None else ["没有拿到 JSON 输出"]
                if not errs:
                    break
                log.info("任务 %s 输出不合格（第 %d 次）：%s", t["id"], attempt + 1, errs[:3])
            else:
                res.ok = False
                res.extra["invalid"] = True
                res.error_text = "输出不符合 Schema：" + "；".join(errs or [])
                return res, None
            if rule.get("worktree") and res.output and res.output.get("status") != "failed":
                self._publish_worktree(wt, res.output, agent, t["id"])
            final = self._finalize(res, schema, run_dir)
            if final[0].ok:
                from .. import ingest

                ingest.apply(rule.get("skill"), final[0].output or {}, lease)
            return final
        finally:
            ctx.close()

    def _publish_worktree(self, wt, output: dict, agent: str, task_id: str) -> None:
        """Agent 提交完：推送 + 开 MR，结果写回输出（mr_url / branch / summary）。"""
        from ..ci import worktree as wtmod

        try:
            pub = wtmod.publish(self.cfg, wt, output, agent, task_id)
        except Exception as e:  # noqa: BLE001
            output["status"] = "failed"
            output["summary"] = f"{output.get('summary', '')}（代码已提交在 {wt.path}，但推送 / 开 MR 失败：{str(e)[:300]}）"[:600]
            return
        if pub["pushed"]:
            output["mr_url"] = pub["mr_url"]
            output["branch"] = wt.branch
            output["summary"] = f"{output.get('summary', '')}（已推送 {wt.branch}，MR：{pub['mr_url']}）"[:600]
            if pub.get("iid"):
                # 直接触发审查，不等 CI 轮询；去重键与轮询一致，轮询再发现也不会重复审查
                output.setdefault("follow_up_events", []).append({
                    "source": "ci", "type": "gitlab.mr.updated" if wt.iid else "gitlab.mr.opened",
                    "subject": f"mr:{wt.project}!{pub['iid']}", "link": pub["mr_url"],
                    "summary": f"MR {wt.project}!{pub['iid']} 有新提交（{agent} 推送）",
                    "dedup_key": f"mr:{wt.project}:{pub['iid']}:{pub['sha']}",
                    "data": {"project": wt.project, "iid": pub["iid"], "sha": pub["sha"], "producer": agent,
                             "web_url": pub["mr_url"], "title": output.get("mr_title")},
                })
        elif pub["dirty"]:
            output["summary"] = f"{output.get('summary', '')}（注意：{wt.path} 有未提交的改动，没有推送）"[:600]

    def _finalize(self, res: RunResult, schema: dict, run_dir: Path) -> tuple[RunResult, Path | None]:
        if not res.ok:
            return res, None
        errs = validate(res.output, schema) if res.output is not None else ["没有拿到 JSON 输出"]
        if errs:
            res.ok = False
            res.extra["invalid"] = True
            res.error_text = "输出不符合 Schema：" + "；".join(errs)
            return res, None
        p = run_dir / "output.json"
        p.write_text(json.dumps(res.output, ensure_ascii=False, indent=1), encoding="utf-8")
        return res, p

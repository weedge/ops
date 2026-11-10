"""opsctl —— 运维命令（09 §5.2）。

  opsctl init                         初始化数据目录、数据库、local.yaml 模板
  opsctl doctor                       检查本机依赖（Agent CLI、dws、ffmpeg、代理、钥匙串）
  opsctl status                       中台、各 Agent、待批卡片、通知队列
  opsctl agents [reset|usage|open] …  Agent 熔断状态；reset 恢复；usage claude 0.75 登记周用量
  opsctl tasks [--state S]            任务列表
  opsctl task <id>                    某任务的完整链路
  opsctl retry <id> [--agent A]       重跑（可指定 Agent）
  opsctl cancel <id>
  opsctl pause <规则|平台|agent|all> / resume …
  opsctl rules check                  校验 rules.json / agents.yaml / schedules.yaml；通过后让中台热加载
  opsctl emit <type> [--source S] [--subject X] [--summary …] [--data JSON] [--dedup K]
  opsctl tick                         立即检查定时事件 / 过期卡片 / 发通知
  opsctl cards [--status S]           卡片列表
  opsctl decide <C12> <选项序号或key> [原因]
  opsctl note "……"                    记下来：追加 decisions.md
  opsctl demand "……"                  人工转入需求
  opsctl published <pk_id> <链接>     半自动发布完成后补登记
  opsctl metrics import <csv>         导入创作者后台导出的数据
  opsctl digest [--hours 24]          中台留底摘要
  opsctl dashboard [--no-open]        生成并打开静态看板
  opsctl launchd install [--load]     生成 launchd plist（默认只生成，不加载）
  opsctl skills link                  把 ops-* Skill 软链接到 ~/.claude/skills 等位置
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from . import config as cfgmod
from . import db, paths
from .opsd.client import HubClient
from .timeutil import iso, now

LOCAL_TEMPLATE = """# 本机配置（不进 git），按同名键覆盖仓库里的 agents.yaml
proxy: {proxy}
notifier:
  adapter: console          # 影子运行先用 console；确认后改成 dingtalk
  dingtalk:
    identity: user
    chat_id: null           # 运营群 openConversationId：dws chat +chat-search --query 运营
    approvers: []           # 允许在群里批红的人：userId（dws contact 查询）
ci:
  gitlab_url: https://gitlab.pri.ibanyu.com
  projects: []              # 例如 popto/frontend_popto
  auto_merge: false         # 先 shadow：合并都发卡片
  base_branch: master
  repos: {{}}                 # GitLab 项目 → 本机仓库路径，例如 front/x/popto: ~/project/palfish/popto/frontend_popto
"""


def client() -> HubClient:
    cfg = cfgmod.load_agents_config()
    h = cfg.get("hub") or {}
    return HubClient(f"http://{h.get('host', '127.0.0.1')}:{h.get('port', 8787)}", h.get("token"), timeout=20)


def out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _need_hub(c: HubClient) -> None:
    if not c.health():
        sys.exit("中台没有运行：launchctl kickstart -k gui/$(id -u)/cn.popto.ops.hub，或前台运行 uv run python -m popto_ops.hub")


# ---------------------------------------------------------------------- init / doctor
def cmd_init(a) -> None:
    root = paths.ensure_data_dirs()
    db.open_hub().close()
    db.open_ops().close()
    local = root / "local.yaml"
    if not local.exists():
        proxy = "null"
        sh = Path("~/claude.sh").expanduser()
        if sh.exists():
            m = re.search(r'PROXY="(http://[^"]+)"', sh.read_text())
            if m:
                proxy = f'"{m.group(1)}"'
        local.write_text(LOCAL_TEMPLATE.format(proxy=proxy), encoding="utf-8")
        local.chmod(0o600)
    print(f"数据目录：{root}\n本机配置：{local}\n下一步：opsctl doctor，然后编辑 local.yaml 里的钉钉群和审批人。")


def cmd_doctor(a) -> None:
    cfg = cfgmod.load_agents_config()
    rows = []
    for name, ac in (cfg.get("agents") or {}).items():
        b = ac.get("bin")
        ok = (Path(os.path.expanduser(b)).exists() if b else ac.get("type") in ("inbox", "script"))
        note = b or ac.get("type")
        if b and b.endswith(".sh"):
            from .opsd.agents import wrapper_proxy
            note += f"（代理 {ac.get('proxy') or wrapper_proxy(b) or '脚本内未写'}）"
        if ac.get("strict_tiers"):
            sb = ac.get("strict_bin")
            ok = ok and bool(sb) and Path(os.path.expanduser(sb)).exists()
            note += f"；{'/'.join(ac['strict_tiers'])} 档位直调 {sb}"
        rows.append((f"Agent {name}", ok, note))
    for tool in ("dws", "ffmpeg", "whisper-cli", "uv", "docker"):
        rows.append((tool, bool(shutil.which(tool)), shutil.which(tool) or "未找到"))
    from .opsd.worker import Worker

    w = Worker(cfg=cfg)
    rows.append(("内网代理", w.probe_proxy(), cfg.get("proxy") or "未配置（Agent 将直连）"))
    for k in ("tikhub", "redfox", "gitlab"):
        from .scriptkit import keychain

        rows.append((f"钥匙串 popto-ops/{k}", bool(keychain(k)), "已配置" if keychain(k) else
                     f"security add-generic-password -s popto-ops/{k} -a popto-ops -w"))
    n = (cfg.get("notifier") or {})
    rows.append(("通知出口", n.get("adapter") != "dingtalk" or bool((n.get("dingtalk") or {}).get("chat_id") or (n.get("dingtalk") or {}).get("user")),
                 n.get("adapter")))
    rep = cfgmod.check_config()
    rows.append(("规则校验", rep.ok, f"{len(rep.errors)} 个错误，{len(rep.warnings)} 个提醒"))
    rows.append(("中台", client().health(), "http://127.0.0.1:8787"))
    st_path = paths.ops_data() / "listener.status.json"
    if (cfg.get("notifier") or {}).get("adapter") == "dingtalk":
        try:
            st = json.loads(st_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        if not st:
            rows.append(("钉钉回复监听", False, "没有状态文件：监听没在跑（python -m popto_ops.notifier.listener）"))
        for k, v in st.items():
            rows.append((f"钉钉监听 {k.replace('user_', '').replace('_triggered', '')[:22]}", bool(v.get("connected")),
                         ("已连接，自 " + str(v.get("since", ""))[11:19]) if v.get("connected")
                         else f"未连接：{str(v.get('last_error') or '启动中')[:80]}"))
    for name, ok, info in rows:
        print(f"{'✓' if ok else '✗'} {name:<24} {info}")


# ---------------------------------------------------------------------- 查询
def cmd_status(a) -> None:
    c = client()
    _need_hub(c)
    out(c.get("/v1/status"))


def cmd_agents(a) -> None:
    c = client()
    _need_hub(c)
    if a.action == "reset":
        out(c.post(f"/v1/agents/{a.agent}/state", {"state": "closed"}))
    elif a.action == "usage":
        out(c.post(f"/v1/agents/{a.agent}/state", {"usage_ratio": float(a.value)}))
    elif a.action == "open":
        out(c.post(f"/v1/agents/{a.agent}/state", {"state": "open", "open_for": a.value or "60m", "error": "人工熔断"}))
    else:
        st = c.get("/v1/status")
        dg = c.get("/v1/digest", since=iso(now() - timedelta(days=7)))
        out({"states": st["agents"], "runs_7d": dg["agent_usage"]})


def cmd_tasks(a) -> None:
    c = client()
    _need_hub(c)
    for t in c.get("/v1/tasks", state=a.state, limit=a.limit)["tasks"]:
        print(f"{t['id']}  {t['state']:<16} {t['rule'][:16]:<16} {t['agent'] or '-':<11} {(t['summary'] or t['error'] or '')[:60]}")


def cmd_task(a) -> None:
    c = client()
    _need_hub(c)
    out(c.get(f"/v1/tasks/{a.id}"))


def cmd_simple_post(path: str, body: dict) -> None:
    c = client()
    _need_hub(c)
    out(c.post(path, body))


def cmd_rules(a) -> None:
    rep = cfgmod.check_config()
    for w in rep.warnings:
        print("提醒：", w)
    for e in rep.errors:
        print("错误：", e)
    if not rep.ok:
        sys.exit(1)
    print(f"通过：{len(cfgmod.load_rules())} 条规则")
    c = client()
    if c.health():
        r = c.post("/v1/rules/reload")
        print("中台已热加载" if r.get("ok") else f"中台加载失败：{r}")


def cmd_emit(a) -> None:
    evt = {"source": a.source, "type": a.type, "subject": a.subject, "summary": a.summary or a.type,
           "link": a.link, "payload_ref": a.payload_ref, "dedup_key": a.dedup}
    if a.data:
        evt["data"] = json.loads(a.data)
    cmd_simple_post("/v1/events", {k: v for k, v in evt.items() if v is not None})


def cmd_cards(a) -> None:
    c = client()
    _need_hub(c)
    for k in c.get("/v1/cards", status=a.status)["cards"]:
        print(f"C{k['seq']:<5} {k['status']:<9} {(k['title'] or '')[:40]:<40} 选择 {k['choice'] or '-'}  过期 {k['expires_at'][5:16]}")


def cmd_digest(a) -> None:
    c = client()
    _need_hub(c)
    out(c.get("/v1/digest", since=iso(now() - timedelta(hours=a.hours))))


def cmd_metrics(a) -> None:
    from .collectors.metrics import main as mmain

    sys.exit(mmain(["import", a.csv]))


def cmd_dashboard(a) -> None:
    from .dashboard import build

    p = build()
    print(p)
    if not a.no_open:
        subprocess.run(["open", str(p)], check=False)


# ---------------------------------------------------------------------- launchd / skills
LAUNCHD = {
    "cn.popto.ops.hub": ["python", "-m", "popto_ops.hub"],
    "cn.popto.ops.opsd": ["python", "-m", "popto_ops.opsd"],
    "cn.popto.ops.dingtalk-events": ["python", "-m", "popto_ops.notifier.listener"],
}


def cmd_launchd(a) -> None:
    tpl = (paths.ops_home() / "launchd" / "template.plist").read_text(encoding="utf-8")
    uv = shutil.which("uv") or str(Path("~/.local/bin/uv").expanduser())
    data = paths.ensure_data_dirs()
    agents_dir = Path("~/Library/LaunchAgents").expanduser()
    cfg = cfgmod.load_agents_config()
    path_env = ":".join(os.path.expanduser(p) for p in str(cfg.get("path", "")).split(":") if p)
    for label, argv in LAUNCHD.items():
        args = [uv, "run", "--project", str(paths.ops_home()), *argv]
        xml = (tpl.replace("{{LABEL}}", label)
                  .replace("{{ARGS}}", "\n".join(f"    <string>{x}</string>" for x in args))
                  .replace("{{PATH}}", path_env).replace("{{OPS_DATA}}", str(data))
                  .replace("{{OPS_HOME}}", str(paths.ops_home())).replace("{{NAME}}", label.split(".")[-1]))
        dst = agents_dir / f"{label}.plist"
        dst.write_text(xml, encoding="utf-8")
        print(f"已生成 {dst}")
        if a.load:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
            r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(dst)], capture_output=True, text=True)
            print(f"  加载 {'成功' if r.returncode == 0 else '失败：' + r.stderr.strip()}")
    if not a.load:
        print("未加载。确认 local.yaml 配好后运行：opsctl launchd install --load")


def cmd_skills_link(a) -> None:
    targets = [Path("~/.claude/skills").expanduser(), paths.ops_home() / ".claude" / "skills", paths.ops_home() / ".agents" / "skills"]
    for t in targets:
        t.mkdir(parents=True, exist_ok=True)
        for sk in sorted((paths.ops_home() / "skills").glob("ops-*")):
            dst = t / sk.name
            if dst.is_symlink() or dst.exists():
                if dst.is_symlink() and dst.resolve() == sk.resolve():
                    continue
                print(f"跳过（已存在）：{dst}")
                continue
            dst.symlink_to(sk)
            print(f"{dst} → {sk}")


# ---------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="opsctl", description="Popto 运营自动化运维命令",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("init").set_defaults(fn=cmd_init)
    sp.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sp.add_parser("status").set_defaults(fn=cmd_status)
    s = sp.add_parser("agents")
    s.add_argument("action", nargs="?", choices=["reset", "usage", "open"])
    s.add_argument("agent", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_agents)
    s = sp.add_parser("tasks")
    s.add_argument("--state")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_tasks)
    s = sp.add_parser("task")
    s.add_argument("id")
    s.set_defaults(fn=cmd_task)
    s = sp.add_parser("retry")
    s.add_argument("id")
    s.add_argument("--agent")
    s.set_defaults(fn=lambda a: cmd_simple_post(f"/v1/tasks/{a.id}/retry", {"agent": a.agent}))
    s = sp.add_parser("cancel")
    s.add_argument("id")
    s.set_defaults(fn=lambda a: cmd_simple_post(f"/v1/tasks/{a.id}/cancel", {}))
    for name in ("pause", "resume"):
        s = sp.add_parser(name)
        s.add_argument("key")
        s.add_argument("--reason", default="")
        s.set_defaults(fn=lambda a, n=name: cmd_simple_post(f"/v1/{n}", {"key": a.key, "reason": a.reason}))
    s = sp.add_parser("rules")
    s.add_argument("action", choices=["check"])
    s.set_defaults(fn=cmd_rules)
    s = sp.add_parser("emit")
    s.add_argument("type")
    s.add_argument("--source", default="opsctl")
    s.add_argument("--subject")
    s.add_argument("--summary")
    s.add_argument("--link")
    s.add_argument("--payload-ref")
    s.add_argument("--data")
    s.add_argument("--dedup")
    s.set_defaults(fn=cmd_emit)
    sp.add_parser("tick").set_defaults(fn=lambda a: cmd_simple_post("/v1/tick", {}))
    s = sp.add_parser("cards")
    s.add_argument("--status")
    s.set_defaults(fn=cmd_cards)
    s = sp.add_parser("decide")
    s.add_argument("card")
    s.add_argument("choice")
    s.add_argument("reason", nargs="*")
    s.set_defaults(fn=lambda a: cmd_simple_post(f"/v1/cards/{a.card}/decide",
                                                {"choice": a.choice, "reason": " ".join(a.reason) or None, "by": os.environ.get("USER", "opsctl")}))
    s = sp.add_parser("note")
    s.add_argument("text", nargs="+")
    s.set_defaults(fn=lambda a: cmd_simple_post("/v1/events", {"source": "opsctl", "type": "decision.note",
                                                               "summary": " ".join(a.text)[:200], "data": {"text": " ".join(a.text)}}))
    s = sp.add_parser("demand")
    s.add_argument("text", nargs="+")
    s.set_defaults(fn=lambda a: cmd_simple_post("/v1/events", {"source": "opsctl", "type": "demand.manual", "subject": "demand:manual",
                                                               "summary": " ".join(a.text)[:300], "data": {"text": " ".join(a.text)}}))
    s = sp.add_parser("published")
    s.add_argument("pack_id")
    s.add_argument("url")
    s.set_defaults(fn=lambda a: cmd_simple_post("/v1/events", {"source": "opsctl", "type": "content.published.manual",
                                                               "subject": f"pack:{a.pack_id}", "summary": f"已发 {a.pack_id}",
                                                               "data": {"pack_id": a.pack_id, "url": a.url}}))
    s = sp.add_parser("metrics")
    s.add_argument("action", choices=["import"])
    s.add_argument("csv")
    s.set_defaults(fn=cmd_metrics)
    s = sp.add_parser("digest")
    s.add_argument("--hours", type=int, default=24)
    s.set_defaults(fn=cmd_digest)
    s = sp.add_parser("dashboard")
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(fn=cmd_dashboard)
    s = sp.add_parser("launchd")
    s.add_argument("action", choices=["install"])
    s.add_argument("--load", action="store_true")
    s.set_defaults(fn=cmd_launchd)
    s = sp.add_parser("skills")
    s.add_argument("action", choices=["link"])
    s.set_defaults(fn=cmd_skills_link)
    a = p.parse_args(argv)
    try:
        a.fn(a)
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()

"""CI 中台脚本（04）：审查结论写回 MR、合并闸门、线上核实。

python -m popto_ops.ci poll           # cron.ci_poll：发现 agent/* 分支的 MR，发 gitlab.mr.opened / updated
python -m popto_ops.ci review-post    # gitlab.review.done：把审查意见写成 MR 评论 + 标签，决定打回还是通过
python -m popto_ops.ci merge-gate     # gitlab.review.passed：跑合并闸门，能自动合就合，否则发卡片
python -m popto_ops.ci merge          # gitlab.mr.merge_approved：人批后合并
python -m popto_ops.ci verify <url> <sha> [--subject mr:repo!iid]   # 线上核实：version.json 是否包含该提交
"""
from __future__ import annotations

import json
import sys
import urllib.request

from ..config import load_agents_config
from ..scriptkit import emit_result, hub_client, load_task
from . import gate
from .. import ingest
from .gitlab import GitLab

MAX_ROUNDS = 3


def _ci_cfg() -> dict:
    c = load_agents_config().get("ci") or {}
    c.setdefault("gitlab_url", "https://gitlab.pri.ibanyu.com")
    c.setdefault("projects", [])
    c.setdefault("auto_merge", False)
    c.setdefault("branch_prefix", "agent/")
    return c


def _producer(mr: dict) -> str:
    desc = mr.get("description") or ""
    for a in ("claude", "codex", "antigravity"):
        if f"producer: {a}" in desc or f"agent:{a}" in (mr.get("labels") or []):
            return a
    return "claude"


def cmd_poll() -> None:
    c = _ci_cfg()
    gl = GitLab(c["gitlab_url"])
    missing = []
    if not c["projects"]:
        missing.append("local.yaml 的 ci.projects 为空")
    if not gl.configured:
        missing.append("钥匙串没有 popto-ops/gitlab（GitLab Token）")
    if missing:
        emit_result("CI 中台未接入：" + "；".join(missing) + "，跳过")
        return
    events = []
    for proj in [GitLab.normalize(p) for p in c["projects"]]:
        for mr in gl.open_mrs(proj):
            if not str(mr.get("source_branch", "")).startswith(c["branch_prefix"]):
                continue
            sha = mr.get("sha")
            labels = mr.get("labels") or []
            subject = f"mr:{proj}!{mr['iid']}"
            if "review:passed" in labels:
                events.append({"source": "ci", "type": "gitlab.review.passed", "subject": subject,
                               "summary": f"MR {proj}!{mr['iid']} 审查通过，待合并闸门",
                               "dedup_key": f"passed:{proj}:{mr['iid']}:{sha}",
                               "data": {"project": proj, "iid": mr["iid"], "sha": sha, "web_url": mr.get("web_url")}})
                continue
            typ = "gitlab.mr.updated" if any(l.startswith("review:round-") for l in labels) else "gitlab.mr.opened"
            events.append({"source": "ci", "type": typ, "subject": subject, "link": mr.get("web_url"),
                           "summary": f"MR {proj}!{mr['iid']}「{mr.get('title', '')[:40]}」有新提交",
                           "dedup_key": f"mr:{proj}:{mr['iid']}:{sha}",
                           "data": {"project": proj, "iid": mr["iid"], "sha": sha, "producer": _producer(mr),
                                    "web_url": mr.get("web_url"), "title": mr.get("title")}})
        # 在 GitLab 上直接合并（没走合并卡片）的 Agent MR：补一条留底，dedup 保证只记一次
        for mr in gl.merged_mrs(proj):
            if not str(mr.get("source_branch", "")).startswith(c["branch_prefix"]):
                continue
            ingest.demand_merged(proj, mr["iid"])  # 幂等：已经 done 的不再改
            events.append({"source": "ci", "type": "gitlab.mr.merged", "subject": f"mr:{proj}!{mr['iid']}",
                           "link": mr.get("web_url"),
                           "summary": f"MR {proj}!{mr['iid']}「{mr.get('title', '')[:40]}」已合并"
                                      f"（{(mr.get('merged_by') or {}).get('username') or '?'} 在 GitLab 上合并）",
                           "dedup_key": f"merged:{proj}:{mr['iid']}",
                           "data": {"project": proj, "iid": mr["iid"], "merged_at": mr.get("merged_at"),
                                    "merged_by": (mr.get("merged_by") or {}).get("username"), "via": "gitlab"}})
    n_open = sum(1 for e in events if e["type"] != "gitlab.mr.merged")
    emit_result(f"发现 {n_open} 个需要处理的 Agent MR", follow_up_events=events)


def _fmt_findings(findings: list[dict], rnd: int) -> str:
    lines = [f"### Agent 审查 · 第 {rnd} 轮", ""]
    if not findings:
        lines.append("没有发现会出错的问题。")
    for f in findings:
        lines.append(f"- **{f.get('severity', 'minor')}** `{f.get('file', '')}:{f.get('line', '')}` {f.get('problem', '')}"
                     + (f"（触发条件：{f['trigger']}）" if f.get("trigger") else ""))
    lines.append("\n_只报真会出错的问题；minor 留档不阻塞。最多 3 轮。_")
    return "\n".join(lines)


def cmd_review_post() -> None:
    lease = load_task()
    c = _ci_cfg()
    gl = GitLab(c["gitlab_url"])
    follow, posted = [], 0
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        proj, iid, rnd = d.get("project"), d.get("iid"), int(d.get("round") or 1)
        findings = d.get("findings") or []
        blocking = [f for f in findings if f.get("severity") in ("blocker", "major")]
        labels = [f"review:round-{rnd}"]
        if not blocking or rnd >= MAX_ROUNDS:
            labels.append("review:passed")
        if gl.configured and proj and iid:
            gl.note(proj, iid, _fmt_findings(findings, rnd))
            gl.add_labels(proj, iid, labels)
            posted += 1
        subject = e.get("subject") or f"mr:{proj}!{iid}"
        if blocking and rnd < MAX_ROUNDS:
            follow.append({"source": "ci", "type": "gitlab.review.changes_requested", "subject": subject,
                           "summary": f"{subject} 第 {rnd} 轮审查有 {len(blocking)} 个需修复的问题",
                           "data": {**d, "findings": blocking}})
        else:
            follow.append({"source": "ci", "type": "gitlab.review.passed", "subject": subject,
                           "summary": f"{subject} 审查通过" + ("（已满 3 轮，剩余意见留档）" if blocking else ""),
                           "data": {"project": proj, "iid": iid, "sha": d.get("sha")}})
    emit_result(f"写回 {posted} 个 MR 的审查结论" if posted else "审查结论未写回 GitLab（未配置 Token），已转后续事件",
                follow_up_events=follow)


def cmd_merge_gate() -> None:
    lease = load_task()
    c = _ci_cfg()
    gl = GitLab(c["gitlab_url"])
    if not gl.configured:
        emit_result("未配置 GitLab Token，无法跑合并闸门")
        return
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        proj, iid = d["project"], d["iid"]
        mr = gl.mr(proj, iid)
        g = gate.check(gl.changes(proj, iid), mr.get("labels"), prd_direction="产品方向" in (mr.get("description") or ""))
        subject = f"mr:{proj}!{iid}"
        if g.blockers:
            gl.note(proj, iid, "### 合并闸门：不能合并\n" + "\n".join(f"- {b}" for b in g.blockers))
            emit_result(f"{subject} 有阻断问题：{'；'.join(g.blockers)[:300]}", follow_up_events=[
                {"source": "ci", "type": "gitlab.review.changes_requested", "subject": subject,
                 "summary": f"{subject} 合并闸门发现阻断问题", "data": {**d, "findings": [{"severity": "blocker", "problem": b} for b in g.blockers]}}])
            return
        if g.auto_merge_ok and c["auto_merge"] and (mr.get("head_pipeline") or mr.get("pipeline") or {}).get("status") == "success":
            gl.merge(proj, iid, mr.get("sha"))
            emit_result(f"{subject} 已自动合并", follow_up_events=[
                {"source": "ci", "type": "gitlab.mr.merged", "subject": subject, "summary": f"{subject} 已合并", "data": d}])
            return
        why = g.needs_human or (["shadow 模式：ci.auto_merge=false"] if not c["auto_merge"] else ["流水线未通过"])
        card = {"kind": "release", "title": f"合并 {proj}!{iid}？", "proposal": "合并（审查已通过）",
                "reasons": [f"MR：{mr.get('web_url')}", f"标题：{mr.get('title')}"], "risks": why,
                "options": [{"key": "merge", "label": "合并",
                             "emit": [{"type": "gitlab.mr.merge_approved", "subject": subject, "data": d}]},
                            {"key": "hold", "label": "先不合", "require_reason": True}],
                "on_expire": "hold", "links": [mr.get("web_url") or ""]}
        emit_result(f"{subject} 需要人批：{'；'.join(why)[:200]}", status="needs_decision", needs_decision=True, decision_card=card)
        return
    emit_result("没有待处理的 MR")


def cmd_merge() -> None:
    lease = load_task()
    c = _ci_cfg()
    gl = GitLab(c["gitlab_url"])
    merged = []
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        if gl.configured:
            gl.merge(d["project"], d["iid"], d.get("sha"))
            merged.append((GitLab.normalize(d["project"]), d["iid"]))
            ingest.demand_merged(GitLab.normalize(d["project"]), d["iid"])
    emit_result(f"已合并 {', '.join(f'{p}!{i}' for p, i in merged)}" if merged else "未合并（未配置 Token）",
                follow_up_events=[{"source": "ci", "type": "gitlab.mr.merged", "subject": f"mr:{p}!{i}",
                                   "summary": f"{p}!{i} 已合并（人批红后由 CI 中台合并）",
                                   "dedup_key": f"merged:{p}:{i}",  # 与 poll 补记的同一把键，避免重复留底
                                   "data": {"project": p, "iid": i, "via": "card"}} for p, i in merged])


def verify(url: str, sha: str) -> tuple[bool, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=20) as r:
            body = r.read().decode(errors="replace")
    except Exception as e:  # noqa: BLE001
        return False, f"请求失败：{e}"
    try:
        data = json.loads(body)
        online = str(data.get("commit") or data.get("sha") or data.get("version") or "")
    except json.JSONDecodeError:
        online = body.strip()[:80]
    ok = bool(online) and (online.startswith(sha[:8]) or sha.startswith(online[:8]))
    return ok, f"线上 {online[:12] or '未知'}，期望 {sha[:12]}"


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "verify" and len(argv) >= 3:
        ok, msg = verify(argv[1], argv[2])
        subject = argv[argv.index("--subject") + 1] if "--subject" in argv else f"deploy:{argv[2][:8]}"
        hub_client().emit({"source": "ci", "type": "deploy.verified" if ok else "deploy.mismatch", "subject": subject,
                           "summary": msg, "link": argv[1], "dedup_key": f"verify:{argv[1]}:{argv[2]}"})
        print(("通过：" if ok else "不一致：") + msg)
        return 0 if ok else 1
    fn = {"poll": cmd_poll, "review-post": cmd_review_post, "merge-gate": cmd_merge_gate, "merge": cmd_merge}.get(cmd)
    if not fn:
        print(__doc__)
        return 2
    fn()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

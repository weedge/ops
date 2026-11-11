"""加载 agents.yaml / rules.json / schedules.yaml，并做配置校验（opsctl rules check）。"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import paths
from .timeutil import parse_duration

TELL = {"never", "needed", "always", "digest"}
TOOLS = {"readonly", "workspace", "browser", "dev"}
EFFORT = {"low", "medium", "high", "xhigh", "max"}
RULE_KEYS = {
    "name", "when", "to", "fallback", "capability", "priority", "cwd", "skill", "ask", "context",
    "effort", "model", "group", "debounce", "parallel", "timeout", "tell", "tools", "max_rounds",
    "command", "note", "credits_per_run", "enabled", "wait_for", "wait_max", "worktree", "overflow_after",
}
CONTEXT_PROVIDERS = {
    "hub_digest", "hub_digest_week", "ops_metrics", "unread_feedback", "demands", "event_payload",
    "event_media", "listening_terms", "budget_popto_credits", "mr_diff", "event_scripts", "popto_help",
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read_yaml(p: Path) -> Any:
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def load_agents_config(home: Path | None = None) -> dict:
    home = home or paths.ops_home()
    base = _read_yaml(home / "agents.yaml") or {}
    local = _read_yaml(paths.ops_data() / "local.yaml") or {}
    return deep_merge(base, local)


def load_rules(home: Path | None = None) -> list[dict]:
    home = home or paths.ops_home()
    rules = json.loads((home / "rules.json").read_text(encoding="utf-8"))
    return [r for r in rules if r.get("enabled", True)]


def load_schedules(home: Path | None = None) -> list[dict]:
    home = home or paths.ops_home()
    return _read_yaml(home / "schedules.yaml") or []


def find_skill(name: str, cfg: dict, home: Path | None = None) -> Path | None:
    """返回 Skill 目录：先找 ops/skills/，再找 external_skill_dirs。"""
    home = home or paths.ops_home()
    cands = [home / "skills" / name]
    for d in cfg.get("external_skill_dirs", []) or []:
        p = Path(d).expanduser()
        if not p.is_absolute():
            p = (home / p).resolve()
        cands.append(p / name)
    for c in cands:
        if (c / "SKILL.md").exists():
            return c
    return None


def rule_chain(rule: dict, cfg: dict) -> list[str]:
    """主 Agent + 降级链（去重保序）。"""
    to = rule.get("to")
    if not to or to == "none":
        return []
    chain = [to]
    fb = rule.get("fallback")
    if fb is None:
        cap = rule.get("capability")
        fb = (cfg.get("fallback_chains") or {}).get(cap, []) if cap else []
    for a in fb:
        if a not in chain:
            chain.append(a)
    return chain


@dataclass
class CheckReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def check_config(home: Path | None = None) -> CheckReport:
    """校验规则与 Agent 配置。改 rules.json 后必须通过才会被中台热加载。"""
    home = home or paths.ops_home()
    rep = CheckReport()
    try:
        cfg = load_agents_config(home)
    except Exception as e:  # noqa: BLE001
        rep.errors.append(f"agents.yaml 解析失败: {e}")
        return rep
    try:
        raw = json.loads((home / "rules.json").read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        rep.errors.append(f"rules.json 解析失败: {e}")
        return rep
    agents = cfg.get("agents") or {}
    for an, ac in agents.items():
        bad = set(ac.get("strict_tiers") or []) - TOOLS
        if bad:
            rep.errors.append(f"agents.{an}.strict_tiers 有未知档位: {sorted(bad)}")
        if ac.get("strict_tiers") and not ac.get("strict_bin"):
            rep.errors.append(f"agents.{an} 写了 strict_tiers 但没写 strict_bin")
    chains = cfg.get("fallback_chains") or {}
    for cap, chain in chains.items():
        for a in chain:
            if a not in agents:
                rep.errors.append(f"fallback_chains.{cap} 引用了未定义的 Agent: {a}")
    if not isinstance(raw, list):
        rep.errors.append("rules.json 顶层必须是数组")
        return rep
    names = set()
    always = 0
    all_names = {r.get("name") for r in raw if isinstance(r, dict)}
    for i, r in enumerate(raw):
        tag = f"规则 #{i}「{r.get('name', '?')}」"
        if isinstance(r, dict):
            for w in r.get("wait_for") or []:
                if w not in all_names:
                    rep.errors.append(f"{tag} wait_for 引用了不存在的规则: {w}")
        if not isinstance(r, dict):
            rep.errors.append(f"{tag} 不是对象")
            continue
        for k in r:
            if k not in RULE_KEYS:
                rep.errors.append(f"{tag} 未知字段: {k}")
        name = r.get("name")
        if not name:
            rep.errors.append(f"{tag} 缺少 name")
        elif name in names:
            rep.errors.append(f"{tag} 规则名重复")
        names.add(name)
        when = r.get("when") or {}
        if not when.get("types"):
            rep.errors.append(f"{tag} when.types 不能为空")
        if "match" in when:
            try:
                re.compile(when["match"])
            except re.error as e:
                rep.errors.append(f"{tag} when.match 正则错误: {e}")
        to = r.get("to")
        if to is None:
            rep.errors.append(f"{tag} 缺少 to")
        elif to != "none" and to not in agents:
            rep.errors.append(f"{tag} to={to} 不在 agents.yaml 里")
        for a in r.get("fallback") or []:
            if a not in agents:
                rep.errors.append(f"{tag} fallback 引用了未定义的 Agent: {a}")
        cap = r.get("capability")
        if cap and cap not in chains:
            rep.errors.append(f"{tag} capability={cap} 没有对应的 fallback_chains")
        tell = r.get("tell", "digest")
        if tell not in TELL:
            rep.errors.append(f"{tag} tell 只能是 {sorted(TELL)}")
        if tell == "always" and r.get("enabled", True):
            always += 1
        if "tools" in r and r["tools"] not in TOOLS:
            rep.errors.append(f"{tag} tools 只能是 {sorted(TOOLS)}")
        if "effort" in r and r["effort"] not in EFFORT:
            rep.errors.append(f"{tag} effort 只能是 {sorted(EFFORT)}")
        for k in ("debounce", "timeout", "overflow_after"):
            if k in r:
                try:
                    parse_duration(r[k])
                except ValueError as e:
                    rep.errors.append(f"{tag} {k}: {e}")
        if "priority" in r and r["priority"] not in (0, 1, 2, 3):
            rep.errors.append(f"{tag} priority 只能是 0–3")
        for c in r.get("context") or []:
            if c not in CONTEXT_PROVIDERS:
                rep.errors.append(f"{tag} 未知 context: {c}")
        if to == "script":
            if not r.get("command"):
                rep.errors.append(f"{tag} to=script 必须写 command")
        elif to not in (None, "none"):
            sk = r.get("skill")
            if not sk:
                rep.errors.append(f"{tag} 调用 Agent 的规则必须写 skill")
            elif not find_skill(sk, cfg, home):
                rep.errors.append(f"{tag} 找不到 Skill: {sk}")
            if not rule_chain(r, cfg):
                rep.errors.append(f"{tag} 没有可用的 Agent 链")
            if r.get("tools") is None:
                rep.warnings.append(f"{tag} 未写 tools，默认按 readonly 运行")
    limit = int(cfg.get("max_tell_always_rules", 4))
    if always > limit:
        rep.errors.append(f"tell=always 的规则有 {always} 条，超过上限 {limit}（会占用太多注意力）")
    # Skill 自身的 schema.json 必须是合法 JSON
    for sk_dir in (home / "skills").glob("ops-*"):
        sp = sk_dir / "schema.json"
        if not (sk_dir / "SKILL.md").exists():
            rep.errors.append(f"Skill {sk_dir.name} 缺少 SKILL.md")
        if sp.exists():
            try:
                json.loads(sp.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                rep.errors.append(f"Skill {sk_dir.name} 的 schema.json 不是合法 JSON: {e}")
    try:
        for s in load_schedules(home):
            if "type" not in s or not ("at" in s or "every" in s):
                rep.errors.append(f"schedules.yaml 条目缺少 type / at / every: {s}")
    except Exception as e:  # noqa: BLE001
        rep.errors.append(f"schedules.yaml 解析失败: {e}")
    return rep

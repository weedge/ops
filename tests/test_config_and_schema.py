import json

from popto_ops import config, scheduler, validate
from popto_ops.timeutil import in_window, parse_duration, window_end

from conftest import at


def test_repo_config_passes(home):
    rep = config.check_config(home)
    assert rep.ok, rep.errors


def test_bad_rule_detected(home):
    rules = json.loads((home / "rules.json").read_text())
    rules.append({"name": "坏规则", "when": {"types": ["x.y"]}, "to": "nobody", "tell": "loud", "skill": "ops-nope",
                  "tools": "root", "unknown": 1})
    (home / "rules.json").write_text(json.dumps(rules, ensure_ascii=False))
    rep = config.check_config(home)
    text = "\n".join(rep.errors)
    assert "to=nobody" in text and "tell" in text and "tools" in text and "未知字段" in text


def test_tell_always_limit(home):
    rules = json.loads((home / "rules.json").read_text())
    for r in rules:
        if r.get("to") == "none":
            r["tell"] = "always"
    (home / "rules.json").write_text(json.dumps(rules, ensure_ascii=False))
    assert any("tell=always" in e for e in config.check_config(home).errors)


def test_fallback_chain_from_capability(home):
    cfg = config.load_agents_config(home)
    rule = {"to": "claude", "capability": "write"}
    assert config.rule_chain(rule, cfg) == ["claude", "codex", "antigravity", "workbuddy"]
    assert config.rule_chain({"to": "claude", "capability": "decide"}, cfg) == ["claude", "codex"]
    assert config.rule_chain({"to": "claude", "fallback": ["workbuddy"]}, cfg) == ["claude", "workbuddy"]


def test_schema_requires_card_when_needs_decision(home):
    sch = validate.build_schema(home / "skills" / "ops-daily-brief")
    out = {"status": "needs_decision", "summary": "x", "needs_decision": True, "message_markdown": "日报正文" * 5}
    errs = validate.validate(out, sch)
    assert any("decision_card" in e for e in errs)
    out["decision_card"] = {"title": "t", "proposal": "p", "options": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]}
    assert validate.validate(out, sch) == []
    del out["message_markdown"]
    assert any("message_markdown" in e for e in validate.validate(out, sch))


def test_all_skill_schemas_compile(home):
    from jsonschema import Draft202012Validator

    for d in (home / "skills").glob("ops-*"):
        Draft202012Validator.check_schema(validate.build_schema(d))


def test_extract_json():
    assert validate.extract_json('前言\n```json\n{"a": 1}\n```') == {"a": 1}
    assert validate.extract_json('结果：{"a": {"b": 2}} 完') == {"a": {"b": 2}}
    assert validate.extract_json("没有") is None


def test_durations_and_windows():
    assert parse_duration("10m").total_seconds() == 600
    assert parse_duration("2h").total_seconds() == 7200
    assert in_window(at("2026-11-10 23:00"), "22:30-08:00")
    assert in_window(at("2026-11-10 07:59"), "22:30-08:00")
    assert not in_window(at("2026-11-10 08:00"), "22:30-08:00")
    assert window_end(at("2026-11-10 23:00"), "22:30-08:00").strftime("%m-%d %H:%M") == "11-11 08:00"


def test_scheduler_last_due():
    now = at("2026-11-11 10:05")  # 周三
    assert scheduler.last_due({"at": "08:30"}, now).strftime("%m-%d %H:%M") == "11-11 08:30"
    assert scheduler.last_due({"at": "17:00"}, now).strftime("%m-%d %H:%M") == "11-10 17:00"
    assert scheduler.last_due({"at": "MON 09:00"}, now).strftime("%m-%d %H:%M") == "11-09 09:00"
    assert scheduler.last_due({"at": "WED 10:00"}, now).strftime("%m-%d %H:%M") == "11-11 10:00"
    assert scheduler.last_due({"at": "WED 11:00"}, now).strftime("%m-%d %H:%M") == "11-04 11:00"
    assert scheduler.last_due({"every": "30m"}, now).strftime("%H:%M") == "10:00"


def test_for_agent_schema_has_no_toplevel_combinators(home):
    sch = validate.for_agent(validate.build_schema(home / "skills" / "ops-comment-triage"))
    text = json.dumps(sch)
    assert "allOf" not in sch and "$ref" not in text and "$defs" not in sch
    assert sch["properties"]["signals"]["items"]["required"] == ["quote", "kind", "module", "intensity"]

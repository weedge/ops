"""写稿 / 合规复核 / 改稿的上下文：共用合规清单、帮助文档、原稿复制、脚本路径统一。"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from popto_ops.opsd import context as ctxmod
from popto_ops.opsd.context import ContextBuilder, load_skill_text, normalize_script_paths


def test_writer_and_reviewer_inline_the_same_checklist(home):
    writer = load_skill_text(home / "skills" / "ops-script-writing")
    reviewer = load_skill_text(home / "skills" / "ops-compliance-review")
    body = (home / "skills" / "ops-compliance-review" / "checklist.md").read_text(encoding="utf-8").strip()
    for text in (writer, reviewer):
        assert "opsd:inline" not in text and body in text
        assert "《人工智能生成合成内容标识办法》第三条" in text and "自拟结构演示" in text


def test_inline_refuses_files_outside_skills_dir(tmp_path):
    sk = tmp_path / "skills" / "ops-x"
    sk.mkdir(parents=True)
    (tmp_path / "secret.md").write_text("不该带进来")
    (sk / "SKILL.md").write_text("开头\n<!-- opsd:inline ../../secret.md -->\n<!-- opsd:inline missing.md -->\n")
    text = load_skill_text(sk)
    assert "不该带进来" not in text and text.count("读不到") == 2


def _lease(event_type: str, data: dict, parent: str | None = None) -> dict:
    return {"task": {"id": "t_now", "attempts": 1, "parent_task": parent}, "rule": {"name": "r"},
            "events": [{"type": event_type, "source": "hub", "subject": "content:c1", "data": data}]}


def _src_scripts(data_dir: Path) -> Path:
    d = data_dir / "runs" / "t_src" / "scripts" / "c1"
    d.mkdir(parents=True)
    (d / "douyin.md").write_text("上一版抖音稿")
    (d / "benchmark.md").write_text("共用的对标说明")
    return d


def test_revise_task_gets_working_copy_of_previous_scripts(home, data_dir):
    _src_scripts(data_dir)
    run_dir = data_dir / "runs" / "t_now"
    ctx = ContextBuilder(None, run_dir)
    ctx.build(["event_scripts"], _lease("content.script.revise", {"script_paths": ["runs/t_src/scripts/c1/douyin.md"]}))
    assert (run_dir / "scripts" / "c1" / "douyin.md").read_text() == "上一版抖音稿"
    assert (run_dir / "scripts" / "c1" / "benchmark.md").exists()  # 同一内容的共用文件一起带上
    assert any(p == "scripts/c1/" and "直接在这些文件上改" in d for p, d in ctx.files)
    ctx.close()


def test_review_task_gets_reference_copy_and_resolves_paths_relative_to_parent_run(home, data_dir):
    _src_scripts(data_dir)
    run_dir = data_dir / "runs" / "t_now"
    ctx = ContextBuilder(None, run_dir)
    ctx.build(["event_scripts"], _lease("content.script.ready", {"script_paths": ["scripts/c1/douyin.md"],
                                                                 "parent_task": "t_src"}))
    assert (run_dir / "context" / "scripts" / "c1" / "douyin.md").exists()
    assert not (run_dir / "scripts").exists()
    assert any(p == "context/scripts/c1/" and "runs/t_src/scripts/c1" in d for p, d in ctx.files)
    ctx.close()


def test_script_paths_outside_runs_are_not_copied(home, data_dir, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("别处的文件")
    run_dir = data_dir / "runs" / "t_now"
    ctx = ContextBuilder(None, run_dir)
    ctx.build(["event_scripts"], _lease("content.script.ready", {"script_paths": [str(outside), "../../secret.md"]}))
    assert not (run_dir / "context" / "scripts").exists()
    assert sum("找不到原稿" in p for p, _ in ctx.files) == 2
    ctx.close()


def test_normalize_script_paths_to_data_root_relative(home, data_dir):
    run_dir = data_dir / "runs" / "t_now"
    (run_dir / "scripts" / "c1").mkdir(parents=True)
    (run_dir / "scripts" / "c1" / "douyin.md").write_text("稿")
    (run_dir / "scripts" / "c1" / "zhihu.md").write_text("稿")
    out = {"scripts": [{"platform": "抖音", "path": "scripts/c1/douyin.md"},
                       {"platform": "知乎", "path": str(run_dir / "scripts" / "c1" / "zhihu.md")},
                       {"platform": "快手", "path": "scripts/c1/kuaishou.md"}],
           "follow_up_events": [{"type": "content.script.ready",
                                 "data": {"script_paths": ["scripts/c1/douyin.md", "runs/t_now/scripts/c1/zhihu.md"]}}]}
    normalize_script_paths(out, run_dir)
    assert [s["path"] for s in out["scripts"]] == ["runs/t_now/scripts/c1/douyin.md", "runs/t_now/scripts/c1/zhihu.md",
                                                   "scripts/c1/kuaishou.md"]  # 不存在的原样留着
    assert out["follow_up_events"][0]["data"]["script_paths"] == ["runs/t_now/scripts/c1/douyin.md",
                                                                  "runs/t_now/scripts/c1/zhihu.md"]


def test_popto_help_docs_copied_without_images(home, data_dir, tmp_path):
    docs = tmp_path / "popto-help"
    (docs / "images").mkdir(parents=True)
    (docs / "README.md").write_text("# 功能地图")
    (docs / "02-数字人口播视频跟创.md").write_text("七步生成")
    (docs / "images" / "a.png").write_bytes(b"png")
    local = yaml.safe_load((data_dir / "local.yaml").read_text())
    local["popto_help_dir"] = str(docs)
    (data_dir / "local.yaml").write_text(yaml.safe_dump(local, allow_unicode=True))
    run_dir = data_dir / "runs" / "t_now"
    ctx = ContextBuilder(None, run_dir)
    ctx.build(["popto_help"], _lease("content.topic.approved", {}))
    got = sorted(p.name for p in (run_dir / "context" / "popto-help").iterdir())
    assert got == ["02-数字人口播视频跟创.md", "README.md"]
    assert any(p == "context/popto-help/" and "2 篇" in d for p, d in ctx.files)
    ctx.close()


def test_popto_help_missing_dir_is_reported_not_fatal(home, data_dir, tmp_path):
    local = yaml.safe_load((data_dir / "local.yaml").read_text())
    local["popto_help_dir"] = str(tmp_path / "nope")
    (data_dir / "local.yaml").write_text(yaml.safe_dump(local, allow_unicode=True))
    ctx = ContextBuilder(None, data_dir / "runs" / "t_now")
    ctx.build(["popto_help"], _lease("content.topic.approved", {}))
    assert any("帮助文档没找到" in p for p, _ in ctx.files)
    ctx.close()


def test_default_help_dir_is_repo_sibling_docs(home):
    assert ctxmod.popto_help_dir({}) == (home / ".." / "docs" / "popto-help").resolve()


def test_content_rules_get_scripts_and_help_context(home):
    rules = {r["name"]: r for r in json.loads((home / "rules.json").read_text(encoding="utf-8"))}
    assert {"event_scripts", "popto_help"} <= set(rules["合规打回后改脚本"]["context"])
    assert {"event_scripts", "popto_help"} <= set(rules["脚本合规复核"]["context"])
    for name in ("选题批准后写脚本", "下周内容日历", "上新内容选题"):
        assert "popto_help" in rules[name]["context"]

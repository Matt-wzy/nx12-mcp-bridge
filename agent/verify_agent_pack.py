#!/usr/bin/env python3
"""Static integrity checks for the bundled nx-agent + Plan Runner pack."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "nx-agent"
RUNNER = ROOT / "agent" / "nx-mcp-plan-runner"

REQUIRED = [
    "SKILL.md",
    "references/text-modeling.md",
    "references/drawing-reader.md",
    "references/modeling-planner.md",
    "references/nx-drawing-rules.md",
    "references/nx-mcp-rules.md",
    "references/topology-safety.md",
    "references/runner-contract.md",
    "references/certified-tool-contract.json",
    "references/pipeline-contract.md",
    "references/chinese-output.md",
    "examples/example-output.json",
    "examples/modeling-plan-example.json",
    "examples/pipeline-state-example.json",
]

OLD_SKILL_NAMES = ("nx-modeling", "nx-engineering-drawing-reader", "nx-mcp-modeling-planner", "nx-mcp-pipeline")


def fail(message: str) -> None:
    raise SystemExit(message)


def main() -> None:
    for rel in REQUIRED:
        if not (SKILL / rel).is_file():
            fail(f"missing nx-agent file: {rel}")

    top = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    match = re.search(r"(?m)^name:\s*(\S+)\s*$", top)
    if not match or match.group(1) != "nx-agent":
        fail("nx-agent frontmatter name mismatch")

    for md in (SKILL / "references").glob("*.md"):
        text = md.read_text(encoding="utf-8")
        if re.search(r"(?m)^name:\s*nx-", text):
            fail(f"standalone Skill frontmatter leaked into {md.name}")
        for old in OLD_SKILL_NAMES:
            if old in text:
                fail(f"legacy Skill name {old!r} remains in {md.name}")

    for rel in ("references/certified-tool-contract.json", "examples/example-output.json", "examples/modeling-plan-example.json", "examples/pipeline-state-example.json"):
        json.loads((SKILL / rel).read_text(encoding="utf-8"))

    for md in [SKILL / "SKILL.md", *(SKILL / "references").glob("*.md")]:
        text = md.read_text(encoding="utf-8")
        for rel in re.findall(r"`((?:references|examples)/[^`\s]+)`", text):
            if not (SKILL / rel).exists():
                fail(f"broken nx-agent reference in {md.name}: {rel}")

    sys.path.insert(0, str(RUNNER))
    import runner  # type: ignore  # noqa: E402

    contract = json.loads((SKILL / "references" / "certified-tool-contract.json").read_text(encoding="utf-8"))
    tools = set(contract["tools"])
    if tools != set(runner.CERTIFIED_TOOLS):
        fail("certified-tool-contract.json and Runner CERTIFIED_TOOLS differ")
    if len(tools) != 32:
        fail(f"expected 32 certified tools, got {len(tools)}")

    runner_examples: dict[str, dict] = {}
    for example in (RUNNER / "examples").glob("*.json"):
        data = json.loads(example.read_text(encoding="utf-8"))
        runner_examples[example.name] = data
        if "skill" in data and data["skill"] != "nx-agent":
            fail(f"runner example has stale skill metadata: {example.name}")

    frozen = runner_examples.get("modeling-plan-example.json")
    executable = runner_examples.get("modeling-plan-executable.json")
    if frozen is None or executable is None:
        fail("runner modeling examples are incomplete")

    if frozen.get("fallbacks") != executable.get("fallbacks"):
        fail("runner frozen/executable fallback safety rules differ")

    def validation_c(data: dict) -> dict:
        for item in data.get("final_validation", []):
            if str(item.get("check", "")).startswith("C."):
                return item
        return {}

    if validation_c(frozen).get("how") != validation_c(executable).get("how"):
        fail("runner frozen/executable final validation C differs")

    def step_56_hole_types(data: dict) -> set[str]:
        for op in data.get("operations", []):
            if op.get("step") == 56:
                face_type = (
                    op.get("selection_criteria", {})
                    .get("holes_reasonable", {})
                    .get("face_type")
                )
                if isinstance(face_type, str):
                    return {face_type}
                if isinstance(face_type, list):
                    return {str(x) for x in face_type}
        return set()

    expected_hole_types = {"Swept", "Cylindrical"}
    if step_56_hole_types(frozen) != expected_hole_types:
        fail("runner frozen example hole-face semantics are stale")
    if step_56_hole_types(executable) != expected_hole_types:
        fail("runner executable example hole-face semantics are stale")

    release_pin_files = [
        SKILL / "SKILL.md",
        *(SKILL / "references").glob("*.md"),
        RUNNER / "README.md",
        RUNNER / "runner.py",
    ]
    for md in release_pin_files:
        if "v2.1.1" in md.read_text(encoding="utf-8"):
            fail(f"stale release pin remains in Agent Pack rules/docs: {md.name}")

    centroid_rule_files = [
        SKILL / "references" / "modeling-planner.md",
        SKILL / "references" / "topology-safety.md",
        SKILL / "references" / "runner-contract.md",
    ]
    for rule_file in centroid_rule_files:
        text = rule_file.read_text(encoding="utf-8")
        if "全局 XY 原点" not in text or "diameter/2" not in text:
            fail(f"centroid_radius global-origin semantics missing in {rule_file.name}")

    output_rules = (SKILL / "references" / "chinese-output.md").read_text(encoding="utf-8")
    if 'report.status == "success"' not in output_rules:
        fail("user-visible success is not bound to Runner report status")

    loader_source = (ROOT / "loader" / "NX_MCP_Loader.cs").read_text(encoding="utf-8")
    principal_plane_tokens = [
        "_sketchPlanes",
        "CreateFixedTypePlane",
        "SketchPlaneMatrix",
        "SketchLocalXAxis",
        "b.PlaneReference = planeRef",
        "b.AxisReference = axisRef",
        "b.SketchOrigin = sketchOrigin",
        "b.PlaneOption = Sketch.PlaneOption.ExistingPlane",
        "b.OriginOption = OriginMethod.SpecifyPoint",
        "SketchPointForPlane",
        'case "XZ": return new Point3d(u, 0.0, v);',
        'case "YZ": return new Point3d(0.0, u, v);',
        "SketchExtrudeAxis",
        'case "XZ": return new Vector3d(0.0, 1.0, 0.0);',
        'case "YZ": return new Vector3d(1.0, 0.0, 0.0);',
    ]
    for token in principal_plane_tokens:
        if token not in loader_source:
            fail(f"principal-plane Loader regression: missing {token}")

    for token in (
        "NXOpen.Part work = _session.Parts.Work;",
        '"<unsaved-active-part>"',
        'return ErrJson("status failed: " + e.Message);',
        '"NX_MCP_WORKSPACE"',
        '"nx_mcp_loader.log"',
    ):
        if token not in loader_source:
            fail(f"Loader stale-part/workspace regression: missing {token}")

    loader_bridge_source = (ROOT / "src" / "nx_mcp" / "loader_bridge.py").read_text(encoding="utf-8")
    for token in ("last_ping_error", "loader status returned ok=false"):
        if token not in loader_bridge_source:
            fail(f"Loader ping diagnostic regression: missing {token}")

    install_agent = (ROOT / "install-agent.ps1").read_text(encoding="utf-8")
    if 'SetEnvironmentVariable("NX_MCP_WORKSPACE", $Workspace, "User")' not in install_agent:
        fail("install-agent.ps1 does not persist NX_MCP_WORKSPACE for Loader")

    if 'Get-Content -Raw -LiteralPath $_ -Encoding UTF8 | ConvertFrom-Json' not in install_agent:
        fail("install-agent.ps1 JSON validation is not PowerShell 5.1 UTF-8 safe")

    plane_rules = (SKILL / "references" / "nx-mcp-rules.md").read_text(encoding="utf-8")
    for token in ("XY→+Z", "XZ→+Y", "YZ→+X", 'nx_extrude(operation="subtract")'):
        if token not in plane_rules:
            fail(f"principal-plane planning contract missing: {token}")

    drawing_reader = (SKILL / "references" / "drawing-reader.md").read_text(encoding="utf-8")
    drawing_rules = (SKILL / "references" / "nx-drawing-rules.md").read_text(encoding="utf-8")
    for token in (
        "正视图 / Front：位于 **XZ** 平面，视图法向轴为 **Y**",
        "width_axis",
        "through_axis",
        "槽宽就是 2",
        "普通线性位置尺寸不得因为数值合适就被改解释成 slot/cut depth",
    ):
        if token not in drawing_reader:
            fail(f"drawing projection/dimension semantics regression: missing {token}")
    for token in (
        "Front/正视图 = XZ 平面，法向轴 Y",
        "两边间距只确定 `width_axis`",
        "不得把附近没有绑定到槽宽的 `1.6` 当成槽宽",
        "普通位置尺寸",
    ):
        if token not in drawing_rules:
            fail(f"quick drawing rules regression: missing {token}")

    drawing_reader = (SKILL / "references" / "drawing-reader.md").read_text(encoding="utf-8")
    drawing_rules = (SKILL / "references" / "nx-drawing-rules.md").read_text(encoding="utf-8")
    pipeline_contract = (SKILL / "references" / "pipeline-contract.md").read_text(encoding="utf-8")
    for token in (
        "HARD / DERIVED / SOFT",
        "required_for_modeling=true",
        "required_for_modeling=false",
        "source_dimensions",
        "blocking_unresolved",
        "规格表字段不要求“逐字段解释完成”才能建模",
    ):
        if token not in drawing_reader:
            fail(f"drawing minimum-closure regression: missing {token}")
    for token in (
        "每个 unresolved 必须标",
        "40+18=58",
        "不要求 warning/soft unresolved=0",
        "只询问 blocking unresolved",
    ):
        if token not in drawing_rules:
            fail(f"quick drawing minimum-closure regression: missing {token}")
    for token in (
        "blocking_unresolved=0",
        "dimension_conflicts=0",
        "required_for_modeling=false",
    ):
        if token not in pipeline_contract:
            fail(f"pipeline Gate A minimum-closure regression: missing {token}")
    for token in (
        "只列出 `required_for_modeling=true`",
        "最少问题",
        "继续 Planner",
    ):
        if token not in output_rules:
            fail(f"Gate A user-output regression: missing {token}")
    for token in (
        "blocking_unresolved = 0",
        "dimension_conflicts = 0",
        "最低充分建模闭合",
    ):
        if token not in top:
            fail(f"top-level Gate A minimum-closure regression: missing {token}")

    for token in (
        'type:"coaxial_hole_group"',
        "同一横向中心线坐标",
        "同组所有 member 继承这一中心线",
        "参数表中的字段名 `C` 与数值 `2`",
    ):
        if token not in drawing_reader:
            fail(f"drawing coaxial/chamfer semantics regression: missing {token}")
    for token in (
        "coaxial_hole_group",
        "同组 member 必须继承同一个 centerline",
        "`C=2` 不等价于边标注 `C2`",
    ):
        if token not in drawing_rules:
            fail(f"quick drawing coaxial/chamfer regression: missing {token}")
    for token in (
        "禁止数值 nudge / epsilon 修复",
        "`Z=50 → Z=49`",
        "不能把这种数值改写当成 Controlled Self-Healing",
    ):
        if token not in pipeline_contract:
            fail(f"pipeline numeric-nudge repair regression: missing {token}")
    for token in (
        "nudge/epsilon",
        "`Z=50 → Z=49`",
        "Boolean 相切失败不能通过改坐标",
    ):
        if token not in top:
            fail(f"top-level numeric-nudge repair regression: missing {token}")

    text_fast = (SKILL / "references" / "text-modeling.md").read_text(encoding="utf-8")
    for token in (
        "建议参数闭合检查",
        "d >= r + R",
        "方位文字与坐标一致",
    ):
        if token not in text_fast:
            fail(f"text-mode clarification geometry guard missing: {token}")

    planner_rules = (SKILL / "references" / "modeling-planner.md").read_text(encoding="utf-8")
    for token in (
        "禁止 Planner 二次猜轴",
        "`axis=X` → YZ sketch → 沿 X subtract",
        "`axis=Y` → XZ sketch → 沿 Y subtract",
        "`through_axis=Y` → XZ sketch → 沿 Y subtract",
    ):
        if token not in planner_rules:
            fail(f"Planner drawing-axis regression: missing {token}")
    for token in (
        "同轴复合孔 centerline 是不可变输入",
        "所有 operation 必须继承组的同一 `axis` 与横向 `centerline`",
        "同一 `coaxial_hole_group` 展开的所有 member operation 的非轴向中心坐标完全一致",
        "孤立参数 `C=2`",
    ):
        if token not in planner_rules:
            fail(f"Planner coaxial/chamfer regression: missing {token}")
    for token in ("澄清建议的几何一致性", "d >= r + R", "方位描述与坐标范围必须一致"):
        if token not in planner_rules:
            fail(f"Planner clarification geometry guard missing: {token}")

    for token in (
        "正常一次通过路径只需要本文件",
        "禁止递归搜索 runtime-config",
        "正常新零件任务**不主动读取**",
        "build/check 必须一次通过；成功后直接进入 Runner",
    ):
        if token not in text_fast:
            fail(f"text-mode Fast Path regression: missing {token}")

    for token in (
        "正常路径只读取",
        "禁止扫描 Skill 目录",
        "正常新零件任务禁止主动读取",
    ):
        if token not in top:
            fail(f"SKILL Mode A Fast Path regression: missing {token}")

    runner_source = (RUNNER / "runner.py").read_text(encoding="utf-8")
    for token in (
        '"nx_modeling_elapsed"',
        '"validation_ops_elapsed"',
        '"export_call_elapsed"',
        '"export_settle_elapsed"',
        '"preflight_elapsed"',
        '"final_validation_elapsed": round(validation_ops_elapsed, 3)',
    ):
        if token not in runner_source:
            fail(f"Runner timing regression: missing {token}")

    if "loader health check failed" not in runner_source or "ping_error()" not in runner_source:
        fail("Runner Loader health-check diagnostics regression")

    plan_tests = (RUNNER / "tests" / "test_plan_resolution.py").read_text(encoding="utf-8")
    if "test_transport_ping_error_passthrough" not in plan_tests:
        fail("Runner Loader ping diagnostic test missing")

    for token in (
        "frozen plan must not contain executable field",
        "frozen plan must not contain executable reference",
        "frozen_errs = check_plan(plan, executable=False)",
    ):
        if token not in runner_source:
            fail(f"Runner frozen/executable boundary regression: missing {token}")

    plan_tests = (RUNNER / "tests" / "test_plan_resolution.py").read_text(encoding="utf-8")
    for token in (
        "test_frozen_check_rejects_executable_only_fields",
        "test_frozen_check_rejects_dollar_references",
    ):
        if token not in plan_tests:
            fail(f"Runner frozen-boundary test missing: {token}")

    if "frozen check/build 对混入 executable 字段的 plan 必须 fail-closed" not in text_fast:
        fail("text-mode frozen/executable boundary rule missing")

    for token in (
        "Selection-consuming params must use the format-specific reference syntax.",
        'value.startswith("$selection.")',
        're.fullmatch(r"<step\\d+[^>]*>", value)',
    ):
        if token not in runner_source:
            fail(f"Runner selection-consumer reference regression: missing {token}")

    for token in (
        "test_frozen_check_rejects_bare_selection_consumer_name",
        "test_executable_check_rejects_unresolved_selection_consumer_name",
    ):
        if token not in plan_tests:
            fail(f"Runner selection-consumer test missing: {token}")

    if "<stepN ...>" not in text_fast or "禁止写裸语义名" not in text_fast:
        fail("text-mode selection consumer placeholder rule missing")

    if "纯计划表达错误" not in pipeline_contract or "result_bindings" not in pipeline_contract:
        fail("pipeline contract does not allow safe one-shot repair of binding-only plan errors")

    timing_tests = (RUNNER / "tests" / "test_bbox_report.py").read_text(encoding="utf-8")
    if "test_timing_bucket_separates_postprocess_from_validation" not in timing_tests:
        fail("Runner timing phase regression test missing")

    for ps1 in (ROOT / "install.ps1", ROOT / "install-agent.ps1"):
        if not ps1.read_bytes().startswith(b"\xef\xbb\xbf"):
            fail(f"{ps1.name} must keep UTF-8 BOM for Windows PowerShell 5.1")

    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
    if 'pip install -e ".[dev]"' in installer:
        fail("normal install.ps1 must not install development extras")
    if 'pip install -e "."' not in installer:
        fail("normal install.ps1 is missing runtime-only editable install")

    print("Agent Pack static verification passed")


if __name__ == "__main__":
    main()

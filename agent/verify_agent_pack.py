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
    ]
    for md in release_pin_files:
        if "v2.1.1" in md.read_text(encoding="utf-8"):
            fail(f"stale release pin remains in Agent Pack rules/docs: {md.name}")

    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
    if 'pip install -e ".[dev]"' in installer:
        fail("normal install.ps1 must not install development extras")
    if 'pip install -e "."' not in installer:
        fail("normal install.ps1 is missing runtime-only editable install")

    print("Agent Pack static verification passed")


if __name__ == "__main__":
    main()

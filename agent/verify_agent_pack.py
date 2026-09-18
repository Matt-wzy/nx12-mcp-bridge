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
FORBIDDEN_CLIENT_WORDS = ("Doubao", "豆包", ".doubao")


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

    product_files = [*SKILL.rglob("*"), ROOT / "install.ps1", ROOT / "install-agent.ps1", ROOT / "README.md", ROOT / "INSTALL.md", ROOT / "docs" / "DRAWING_TO_NX.md"]
    for path in product_files:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for word in FORBIDDEN_CLIENT_WORDS:
            if word in text:
                fail(f"client-specific word {word!r} found in {path.relative_to(ROOT)}")

    for rel in ("references/certified-tool-contract.json", "examples/example-output.json", "examples/modeling-plan-example.json", "examples/pipeline-state-example.json"):
        json.loads((SKILL / rel).read_text(encoding="utf-8"))

    for md in [SKILL / "SKILL.md", *(SKILL / "references").glob("*.md")]:
        text = md.read_text(encoding="utf-8")
        for rel in re.findall(r"`((?:references|examples)/[^`\s]+)`", text):
            if not (SKILL / rel).exists():
                fail(f"broken nx-agent reference in {md.name}: {rel}")

    sys.path.insert(0, str(RUNNER))
    import runner  # type: ignore

    contract = json.loads((SKILL / "references" / "certified-tool-contract.json").read_text(encoding="utf-8"))
    tools = set(contract["tools"])
    if tools != set(runner.CERTIFIED_TOOLS):
        fail("certified-tool-contract.json and Runner CERTIFIED_TOOLS differ")
    if len(tools) != 32:
        fail(f"expected 32 certified tools, got {len(tools)}")

    print("Agent Pack static verification passed")


if __name__ == "__main__":
    main()

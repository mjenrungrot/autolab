#!/usr/bin/env python3
"""Verify that stage prompts document all required_outputs from workflow.yaml."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from verifier_lib import REPO_ROOT, load_yaml, make_result, print_result

PROMPTS_DIR = REPO_ROOT / ".autolab" / "prompts"
WORKFLOW_PATH = REPO_ROOT / ".autolab" / "workflow.yaml"
OUTPUTS_SECTION_PATTERN = re.compile(
    r"## OUTPUTS \(STRICT\)\s*\n(.*?)(?=\n## |\Z)",
    re.DOTALL,
)


def _check_stage(stage: str, spec: dict, prompts_dir: Path) -> list[str]:
    prompt_file = spec.get("prompt_file", "")
    if not prompt_file:
        return []
    prompt_path = prompts_dir / prompt_file
    if not prompt_path.exists():
        return [f"{prompt_path} is missing"]

    required_outputs = spec.get("required_outputs", [])
    if not isinstance(required_outputs, list):
        required_outputs = []

    required_outputs_any_of = spec.get("required_outputs_any_of", [])
    if not isinstance(required_outputs_any_of, list):
        required_outputs_any_of = []

    required_outputs_if = spec.get("required_outputs_if", [])
    if isinstance(required_outputs_if, dict):
        required_outputs_if = [required_outputs_if]
    if not isinstance(required_outputs_if, list):
        required_outputs_if = []

    if not required_outputs and not required_outputs_any_of and not required_outputs_if:
        return []

    text = prompt_path.read_text(encoding="utf-8")
    match = OUTPUTS_SECTION_PATTERN.search(text)
    if not match:
        # Auto-injection at render time will supply this section, so pass
        return []

    outputs_text = match.group(1)

    def _mentions_output(raw_output: str) -> bool:
        normalized = str(raw_output).replace("<RUN_ID>", "").replace("{{run_id}}", "")
        filename = Path(normalized).name
        return bool(
            filename and (filename in outputs_text or str(raw_output) in outputs_text)
        )

    failures: list[str] = []
    for output in required_outputs:
        if not _mentions_output(str(output)):
            failures.append(
                f"{prompt_path} OUTPUTS section does not mention required output '{output}' from workflow.yaml"
            )

    for group in required_outputs_any_of:
        if not isinstance(group, list) or not group:
            continue
        normalized_group = [str(item).strip() for item in group if str(item).strip()]
        if not normalized_group:
            continue
        if not any(_mentions_output(item) for item in normalized_group):
            failures.append(
                f"{prompt_path} OUTPUTS section does not mention any required one-of outputs: {normalized_group}"
            )

    for rule in required_outputs_if:
        if not isinstance(rule, dict):
            continue
        outputs = rule.get("outputs", [])
        if not isinstance(outputs, list):
            continue
        for output in outputs:
            output_text = str(output).strip()
            if not output_text:
                continue
            if not _mentions_output(output_text):
                failures.append(
                    f"{prompt_path} OUTPUTS section does not mention conditional required output '{output_text}' from workflow.yaml"
                )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default=None, help="Stage to check (default: all)")
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable JSON envelope",
    )
    args = parser.parse_args()

    try:
        workflow = load_yaml(WORKFLOW_PATH)
    except Exception as exc:
        result = make_result(
            "prompt_registry_contract", args.stage or "", [], [str(exc)]
        )
        print_result(result, as_json=args.json)
        return 1

    stages_config = workflow.get("stages", {})
    if not isinstance(stages_config, dict):
        result = make_result(
            "prompt_registry_contract",
            "",
            [],
            ["workflow.yaml stages must be a mapping"],
        )
        print_result(result, as_json=args.json)
        return 1

    failures: list[str] = []
    if args.stage:
        requested = str(args.stage).strip()
        if requested not in stages_config:
            result = make_result(
                "prompt_registry_contract",
                requested,
                [],
                [f"unknown stage '{requested}'"],
            )
            print_result(result, as_json=args.json)
            return 1
        failures.extend(_check_stage(requested, stages_config[requested], PROMPTS_DIR))
    else:
        for stage_name, spec in stages_config.items():
            if isinstance(spec, dict):
                failures.extend(_check_stage(stage_name, spec, PROMPTS_DIR))

    checks = [{"name": f, "status": "fail", "detail": f} for f in failures]
    if not failures:
        checks = [
            {
                "name": "prompt_registry_contract",
                "status": "pass",
                "detail": "all prompts cover registry outputs",
            }
        ]
    result = make_result("prompt_registry_contract", args.stage or "", checks, failures)
    print_result(result, as_json=args.json)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

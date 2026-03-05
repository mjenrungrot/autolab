from __future__ import annotations

from typing import Any


def parse_results(
    *,
    repo_root: str,
    iteration_dir: str,
    run_id: str,
    state: dict[str, Any],
    design: dict[str, Any],
) -> dict[str, Any]:
    _ = repo_root, iteration_dir, state, design
    return {
        "metrics": {
            "schema_version": "1.0",
            "iteration_id": "iter_fixture_python",
            "run_id": run_id,
            "status": "completed",
            "primary_metric": {
                "name": "validation_loss",
                "value": 0.18,
                "delta_vs_baseline": -0.04,
            },
        },
        "summary_markdown": (
            "# Analysis Summary\n\n"
            "- fixture: python_basic\n"
            f"- run_id: {run_id}\n"
            "- validation_loss: 0.18\n"
        ),
    }

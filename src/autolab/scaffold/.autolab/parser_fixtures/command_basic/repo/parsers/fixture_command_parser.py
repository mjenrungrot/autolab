from __future__ import annotations

import argparse
import json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="command fixture parser")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--iteration-path", required=True)
    args = parser.parse_args(argv)

    payload = {
        "metrics": {
            "schema_version": "1.0",
            "iteration_id": "iter_fixture_command",
            "run_id": str(args.run_id),
            "status": "completed",
            "primary_metric": {
                "name": "validation_accuracy",
                "value": 0.82,
                "delta_vs_baseline": 0.02,
            },
        },
        "summary_markdown": (
            "# Analysis Summary\n\n"
            "- fixture: command_basic\n"
            f"- run_id: {args.run_id}\n"
            "- validation_accuracy: 0.82\n"
        ),
    }
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

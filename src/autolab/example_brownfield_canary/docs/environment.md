# Environment

This sanitized canary models a repo-level environment guide that operators read
before running an in-progress experiment.

## Assumptions

- Python 3.11+ is available.
- A local virtual environment lives at `.venv/`.
- The bootstrap flow is driven by `scripts/bootstrap_venv.sh`.

## Notes

- This file is intentionally project-wide so mixed-scope planning can exercise
  plan approval and UAT behavior.

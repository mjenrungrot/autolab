This packaged fixture is a sanitized, Tinydesk-like brownfield research repo.

It is intentionally small, but it exercises:

- `.autolab/` repo-local state and planning artifacts
- `docs/environment.md` and `scripts/bootstrap_venv.sh`
- `experiments/in_progress/iter_brownfield_canary/...`
- mixed-scope implementation planning with promoted experiment context
- plan approval plus UAT artifacts
- remote profile diagnostics on a deterministic shared-fs profile

The integration test copies this repo into a temp git worktree, runs
`autolab sync-scaffold --force`, and then executes a realistic command chain
against the copied fixture.

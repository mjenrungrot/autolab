# User Acceptance Test

UATStatus: pass

## Scope

- iteration_id: iter_brownfield_canary
- scope_kind: project_wide
- required_by: policy

## Preconditions

- revision_label: fixture
- host_mode: local
- remote_profile: local_shared

## Checks

### Check 1 - bootstrap instructions stay aligned

- command: compare `docs/environment.md` with `scripts/bootstrap_venv.sh`
- expected: the doc describes the same bootstrap entrypoint used by the script
- observed: both artifacts point to the same `.venv` bootstrap flow
- result: pass

## Follow-ups

- none

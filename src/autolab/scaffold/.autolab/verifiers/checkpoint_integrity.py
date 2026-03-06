"""Checkpoint integrity verifier: validates index, manifests, and fingerprints."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys

_VERIFIER_DIR = pathlib.Path(__file__).resolve().parent
if str(_VERIFIER_DIR) not in sys.path:
    sys.path.insert(0, str(_VERIFIER_DIR))

from verifier_lib import load_json, load_state, make_result, print_result

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / ".autolab" / "schemas"


def _validate_index(index_path: pathlib.Path) -> list[str]:
    errors: list[str] = []
    if not index_path.exists():
        return []  # No checkpoints yet is fine

    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"index.json parse error: {exc}"]

    if not isinstance(index, dict):
        return ["index.json must be an object"]
    if index.get("schema_version") != "1.0":
        errors.append(f"unexpected schema_version: {index.get('schema_version')}")
    checkpoints = index.get("checkpoints")
    if not isinstance(checkpoints, list):
        errors.append("index.checkpoints must be an array")
        return errors

    schema_path = SCHEMA_DIR / "checkpoint_index.schema.json"
    if schema_path.exists():
        try:
            from jsonschema import Draft202012Validator

            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            validator = Draft202012Validator(schema)
            for error in validator.iter_errors(index):
                path_str = ".".join(str(p) for p in error.absolute_path)
                errors.append(f"index schema: $.{path_str}: {error.message}")
        except ImportError:
            pass

    return errors


def _validate_manifests(checkpoints_dir: pathlib.Path, index: dict) -> list[str]:
    errors: list[str] = []
    entries = index.get("checkpoints", [])
    if not isinstance(entries, list):
        return errors

    for entry in entries:
        cp_id = entry.get("checkpoint_id", "")
        cp_dir = checkpoints_dir / cp_id
        manifest_path = cp_dir / "manifest.json"
        if not manifest_path.exists():
            errors.append(f"manifest.json missing for indexed checkpoint {cp_id}")

    return errors


def _spot_check_fingerprints(checkpoints_dir: pathlib.Path, index: dict) -> list[str]:
    errors: list[str] = []
    entries = index.get("checkpoints", [])
    if not isinstance(entries, list):
        return errors

    recent = entries[-3:] if len(entries) > 3 else entries
    for entry in recent:
        cp_id = entry.get("checkpoint_id", "")
        cp_dir = checkpoints_dir / cp_id
        manifest_path = cp_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        artifacts = manifest.get("artifacts", [])
        if not isinstance(artifacts, list):
            continue
        files_dir = cp_dir / "files"
        for art in artifacts:
            rel = art.get("relative_path", "")
            expected = art.get("fingerprint", "")
            stored = files_dir / rel
            if not stored.exists():
                errors.append(f"{cp_id}: stored file missing: {rel}")
                continue
            if expected and expected not in ("<missing>", "<dir>", "<unreadable>"):
                actual = hashlib.sha1(stored.read_bytes()).hexdigest()
                if actual != expected:
                    errors.append(f"{cp_id}: fingerprint mismatch for {rel}")
    return errors


def _check_orphans(checkpoints_dir: pathlib.Path, index: dict) -> list[str]:
    errors: list[str] = []
    if not checkpoints_dir.is_dir():
        return errors
    indexed_ids = set()
    entries = index.get("checkpoints", [])
    if isinstance(entries, list):
        indexed_ids = {e.get("checkpoint_id", "") for e in entries}
    for child in checkpoints_dir.iterdir():
        if (
            child.is_dir()
            and child.name.startswith("cp_")
            and child.name not in indexed_ids
        ):
            errors.append(f"orphaned checkpoint directory: {child.name}")
    return errors


def run_checks(*, as_json: bool = False) -> dict:
    state = load_state()
    stage = state.get("stage", "")

    checkpoints_dir = REPO_ROOT / ".autolab" / "checkpoints"
    index_path = checkpoints_dir / "index.json"

    all_errors: list[str] = []
    checks: list[dict] = []

    # Check 1: Index validation
    index_errors = _validate_index(index_path)
    checks.append(
        {
            "check": "index_schema",
            "passed": len(index_errors) == 0,
            "errors": index_errors,
        }
    )
    all_errors.extend(index_errors)

    # Load index for remaining checks
    index: dict = {}
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Check 2: Manifest presence
    manifest_errors = _validate_manifests(checkpoints_dir, index)
    checks.append(
        {
            "check": "manifest_presence",
            "passed": len(manifest_errors) == 0,
            "errors": manifest_errors,
        }
    )
    all_errors.extend(manifest_errors)

    # Check 3: Fingerprint spot-check
    fp_errors = _spot_check_fingerprints(checkpoints_dir, index)
    checks.append(
        {
            "check": "fingerprint_integrity",
            "passed": len(fp_errors) == 0,
            "errors": fp_errors,
        }
    )
    all_errors.extend(fp_errors)

    # Check 4: Orphan detection
    orphan_errors = _check_orphans(checkpoints_dir, index)
    checks.append(
        {
            "check": "orphan_detection",
            "passed": len(orphan_errors) == 0,
            "errors": orphan_errors,
        }
    )
    all_errors.extend(orphan_errors)

    result = make_result(
        verifier="checkpoint_integrity",
        stage=stage,
        checks=checks,
        errors=all_errors,
    )
    print_result(result, as_json=as_json)
    return result


if __name__ == "__main__":
    as_json = "--json" in sys.argv
    result = run_checks(as_json=as_json)
    sys.exit(0 if not result.get("errors") else 1)

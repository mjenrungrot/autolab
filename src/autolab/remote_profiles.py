from __future__ import annotations

import fnmatch
import json
import shlex
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import yaml
except Exception:
    yaml = None

from autolab.constants import DEFAULT_PROFILE_MODE
from autolab.models import (
    RemoteArtifactPullConfig,
    RemoteDataPolicyConfig,
    RemoteEnvConfig,
    RemoteGitSyncConfig,
    RemoteHostDetectionConfig,
    RemoteProfileConfig,
    RemoteProfilesConfig,
    RevisionLabelInfo,
    StageCheckError,
    _coerce_bool,
    _coerce_float,
)
from autolab.utils import _load_json_if_exists

_VALID_PROFILE_MODES = {"shared_fs", "git_checkout", "verify_only"}
_VALID_LOCAL_SYNC_MODES = {"allowed", "forbidden"}
_UNSAFE_COMMAND_FRAGMENTS = ("&&", "||", ";", "|", "`", "$(", "\n", "\r", ">", "<")


def normalize_profile_mode(raw_mode: str) -> str:
    candidate = str(raw_mode or "").strip().lower()
    if not candidate:
        return DEFAULT_PROFILE_MODE
    if candidate == "standalone":
        return "git_checkout"
    if candidate in _VALID_PROFILE_MODES:
        return candidate
    raise StageCheckError(
        f"Unknown remote profile mode '{candidate}'; "
        "profile_mode must be one of shared_fs, git_checkout, verify_only, or legacy standalone"
    )


def normalize_host_mode(host_mode: str) -> str:
    candidate = str(host_mode or "").strip().lower()
    if candidate == "slurm_interactive":
        return "slurm"
    return candidate


def load_remote_profiles(repo_root: Path) -> RemoteProfilesConfig:
    path = repo_root / ".autolab" / "remote_profiles.yaml"
    if yaml is None or not path.exists():
        return RemoteProfilesConfig(
            schema_version="1.0",
            path=path,
            default_profile="",
            profiles={},
        )
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StageCheckError(
            f"invalid remote profiles config at {path}: {exc}"
        ) from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise StageCheckError(f"remote profiles config at {path} must be a mapping")
    profiles_raw = loaded.get("profiles")
    if not isinstance(profiles_raw, dict):
        profiles_raw = {}
    profiles: dict[str, RemoteProfileConfig] = {}
    for raw_name, raw_profile in profiles_raw.items():
        if not isinstance(raw_profile, dict):
            raise StageCheckError(
                f"remote profile '{raw_name}' in {path} must be a mapping"
            )
        name = str(raw_name).strip()
        if not name:
            raise StageCheckError(f"remote profile names in {path} must be non-empty")
        profiles[name] = _parse_remote_profile(name, raw_profile)
    default_profile = str(loaded.get("default_profile", "")).strip()
    if default_profile and default_profile not in profiles:
        raise StageCheckError(
            f"default_profile '{default_profile}' is not defined in {path}"
        )
    return RemoteProfilesConfig(
        schema_version=str(loaded.get("schema_version", "1.0")).strip() or "1.0",
        path=path,
        default_profile=default_profile,
        profiles=profiles,
    )


def resolve_remote_profile(
    repo_root: Path,
    *,
    host_mode: str = "",
    profile_name: str = "",
) -> RemoteProfileConfig:
    config = load_remote_profiles(repo_root)
    selected = str(profile_name or config.default_profile).strip()
    if not selected:
        raise StageCheckError(
            "no remote profile selected; set .autolab/remote_profiles.yaml default_profile"
        )
    profile = config.profiles.get(selected)
    if profile is None:
        raise StageCheckError(f"remote profile '{selected}' is not defined")
    normalized_host_mode = normalize_host_mode(host_mode)
    if (
        normalized_host_mode
        and profile.enabled_for_host_modes
        and normalized_host_mode not in profile.enabled_for_host_modes
    ):
        raise StageCheckError(
            f"remote profile '{selected}' is not enabled for host_mode={normalized_host_mode}"
        )
    return profile


def resolve_workspace_revision(repo_root: Path) -> RevisionLabelInfo:
    dirty = True
    label = ""
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        dirty = bool(status.returncode != 0 or str(status.stdout).strip())
    except Exception:
        dirty = True
    try:
        describe = subprocess.run(
            ["git", "describe", "--tags", "--exact-match"],
            cwd=str(repo_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        if describe.returncode == 0 and str(describe.stdout).strip():
            label = str(describe.stdout).strip()
    except Exception:
        label = ""
    return RevisionLabelInfo(label=label, source="git_tag", dirty=dirty)


def ensure_remote_launch_revision(
    repo_root: Path, profile: RemoteProfileConfig
) -> RevisionLabelInfo:
    revision = resolve_workspace_revision(repo_root)
    if profile.git_sync.revision_source != "git_tag":
        raise StageCheckError(
            "remote launch currently supports only git_tag revision_source"
        )
    if profile.git_sync.require_clean_worktree and revision.dirty:
        raise StageCheckError(
            "remote launch requires a clean, version-labeled revision; worktree is dirty"
        )
    if not revision.label:
        raise StageCheckError(
            "remote launch requires HEAD to be tagged exactly with a git tag"
        )
    try:
        remote_tag = subprocess.run(
            ["git", "ls-remote", "--tags", "origin", f"refs/tags/{revision.label}"],
            cwd=str(repo_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except Exception as exc:
        raise StageCheckError(
            f"failed to verify tag '{revision.label}' on origin: {exc}"
        ) from exc
    if remote_tag.returncode != 0 or not str(remote_tag.stdout).strip():
        raise StageCheckError(
            f"remote launch requires tag '{revision.label}' to exist on origin"
        )
    return revision


def ensure_remote_profile_launch_ready(profile: RemoteProfileConfig) -> None:
    issues = lint_remote_profile(profile)
    for command in profile.host_detection.require_commands:
        if shutil.which(command) is None:
            issues.append(f"required host command missing: {command}")
    if profile.mode != "shared_fs" and shutil.which("ssh") is None:
        issues.append("required host command missing: ssh")
    if issues:
        raise StageCheckError("; ".join(issues))


def workspace_revision_payload(repo_root: Path) -> dict[str, Any]:
    revision = resolve_workspace_revision(repo_root)
    return {
        "label": revision.label,
        "source": revision.source,
        "dirty": revision.dirty,
    }


def build_remote_execution_payload(
    profile: RemoteProfileConfig,
    *,
    requested_revision_label: str,
    status: str,
    resolved_remote_revision_label: str = "",
) -> dict[str, Any]:
    return {
        "profile": profile.name,
        "mode": profile.mode,
        "remote_repo_root": profile.remote_repo_root,
        "code_sync": {
            "requested_revision_label": requested_revision_label,
            "resolved_remote_revision_label": (
                resolved_remote_revision_label or requested_revision_label
            ),
            "status": str(status).strip().lower() or "unknown",
        },
    }


def remote_path_for(
    profile: RemoteProfileConfig, repo_root: Path, local_path: Path
) -> str:
    relative = local_path.resolve().relative_to(repo_root.resolve()).as_posix()
    return str(PurePosixPath(profile.remote_repo_root) / PurePosixPath(relative))


def run_remote_command(
    profile: RemoteProfileConfig,
    command: str | list[str],
    *,
    cwd: str = "",
    timeout_seconds: float = 30.0,
    capture_output: bool = True,
    text: bool = True,
    env: dict[str, str] | None = None,
    allow_compound: bool = False,
) -> subprocess.CompletedProcess:
    segments = ["set -euo pipefail"]
    if cwd:
        segments.append(f"cd {shlex.quote(cwd)}")
    command_segment = _command_segment(
        command, env=env or {}, allow_compound=allow_compound
    )
    segments.append(command_segment)
    script = "; ".join(segment for segment in segments if segment)
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", profile.login_host, "bash", "-lc", script],
        text=text,
        capture_output=capture_output,
        check=False,
        timeout=timeout_seconds,
    )


def ensure_remote_python(
    profile: RemoteProfileConfig, *, timeout_seconds: float = 120.0
) -> None:
    check = run_remote_command(
        profile,
        [profile.python_path, "-V"],
        cwd=profile.remote_repo_root,
        timeout_seconds=timeout_seconds,
    )
    if check.returncode == 0:
        return
    if not profile.bootstrap_command:
        raise StageCheckError(
            f"remote python_path '{profile.python_path}' is not runnable and no bootstrap_command is configured"
        )
    bootstrap = run_remote_command(
        profile,
        _parse_command_template(profile.bootstrap_command, context="bootstrap_command"),
        cwd=profile.remote_repo_root,
        timeout_seconds=timeout_seconds,
    )
    if bootstrap.returncode != 0:
        stderr = str(bootstrap.stderr or "").strip()
        raise StageCheckError(
            f"remote bootstrap_command failed on {profile.login_host}: {stderr or 'unknown error'}"
        )
    verify = run_remote_command(
        profile,
        [profile.python_path, "-V"],
        cwd=profile.remote_repo_root,
        timeout_seconds=timeout_seconds,
    )
    if verify.returncode != 0:
        stderr = str(verify.stderr or "").strip()
        raise StageCheckError(
            f"remote python_path '{profile.python_path}' is still not runnable after bootstrap: {stderr or 'unknown error'}"
        )


def verify_remote_checkout(
    profile: RemoteProfileConfig,
    revision_label: str,
    *,
    timeout_seconds: float = 60.0,
) -> str:
    proc = run_remote_command(
        profile,
        ["git", "describe", "--tags", "--exact-match"],
        cwd=profile.remote_repo_root,
        timeout_seconds=timeout_seconds,
    )
    if proc.returncode != 0:
        raise StageCheckError(
            "remote checkout verification failed: HEAD is not tagged exactly"
        )
    resolved = str(proc.stdout or "").strip()
    if resolved != revision_label:
        raise StageCheckError(
            f"remote checkout verification failed: expected {revision_label}, found {resolved or 'unversioned'}"
        )
    return resolved


def fetch_and_checkout_remote_revision(
    profile: RemoteProfileConfig,
    revision_label: str,
    *,
    timeout_seconds: float = 120.0,
) -> str:
    fetch_argv = _parse_command_template(
        profile.git_sync.fetch_command,
        context="git_sync.fetch_command",
    )
    checkout_argv = _parse_command_template(
        profile.git_sync.checkout_command,
        context="git_sync.checkout_command",
        substitutions={"revision_label": revision_label},
    )
    describe_argv = ["git", "describe", "--tags", "--exact-match"]
    proc = run_remote_command(
        profile,
        _command_chain(fetch_argv, checkout_argv, describe_argv),
        cwd=profile.remote_repo_root,
        timeout_seconds=timeout_seconds,
        allow_compound=True,
    )
    if proc.returncode != 0:
        stderr = str(proc.stderr or "").strip()
        raise StageCheckError(
            f"remote checkout to {revision_label} failed on {profile.login_host}: {stderr or 'unknown error'}"
        )
    resolved_lines = [
        line.strip() for line in str(proc.stdout or "").splitlines() if line.strip()
    ]
    return resolved_lines[-1] if resolved_lines else revision_label


def submit_remote_slurm_job(
    profile: RemoteProfileConfig,
    *,
    repo_root: Path,
    iteration_dir: Path,
    run_id: str,
    iteration_id: str,
    revision_label: str,
    timeout_seconds: float,
) -> tuple[str, str, str, str]:
    if profile.mode == "git_checkout":
        fetch_and_checkout_remote_revision(
            profile, revision_label, timeout_seconds=timeout_seconds
        )
    elif profile.mode == "verify_only":
        verify_remote_checkout(profile, revision_label, timeout_seconds=timeout_seconds)
    ensure_remote_python(profile, timeout_seconds=timeout_seconds)
    remote_iteration_dir = remote_path_for(profile, repo_root, iteration_dir)
    export_value = f"ALL,RUN_ID={run_id},AUTOLAB_RUN_ID={run_id},AUTOLAB_ITERATION_ID={iteration_id}"
    cache_keys = sorted(profile.env.cache_vars)
    if cache_keys:
        export_value = f"{export_value},{','.join(cache_keys)}"
    submit_argv = _parse_command_template(
        profile.submit_command,
        context="submit_command",
    ) + [f"--export={export_value}", "launch/run_slurm.sbatch"]
    submit_env = _remote_env_map(profile)
    proc = run_remote_command(
        profile,
        submit_argv,
        cwd=remote_iteration_dir,
        timeout_seconds=timeout_seconds,
        env=submit_env,
    )
    command_text = _remote_provenance_text(
        profile=profile,
        cwd=remote_iteration_dir,
        argv=submit_argv,
        env=submit_env,
    )
    return (
        str(proc.stdout or ""),
        str(proc.stderr or ""),
        remote_iteration_dir,
        command_text,
    )


def poll_remote_job(
    profile: RemoteProfileConfig,
    *,
    job_id: str,
    timeout_seconds: float,
) -> tuple[str, str]:
    proc = run_remote_command(
        profile,
        ["squeue", "-j", job_id, "-h", "-o", "%T"],
        cwd=profile.remote_repo_root,
        timeout_seconds=timeout_seconds,
    )
    if proc.returncode != 0:
        raise StageCheckError(
            f"remote poll failed for job_id={job_id} on {profile.login_host}"
        )
    return (str(proc.stdout or ""), str(proc.stderr or ""))


def cancel_remote_job(
    profile: RemoteProfileConfig,
    *,
    job_id: str,
    timeout_seconds: float,
) -> tuple[str, str]:
    proc = run_remote_command(
        profile,
        ["scancel", job_id],
        cwd=profile.remote_repo_root,
        timeout_seconds=timeout_seconds,
    )
    if proc.returncode != 0:
        raise StageCheckError(
            f"remote cancel failed for job_id={job_id} on {profile.login_host}"
        )
    return (str(proc.stdout or ""), str(proc.stderr or ""))


def pull_remote_artifacts(
    profile: RemoteProfileConfig,
    *,
    repo_root: Path,
    iteration_id: str,
    run_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    if profile.data_policy.local_sync not in _VALID_LOCAL_SYNC_MODES:
        return {
            "status": "failed",
            "pulled_paths": [],
            "failures": [
                {
                    "path": "",
                    "reason": f"unsupported local_sync policy '{profile.data_policy.local_sync}'",
                }
            ],
        }
    if not profile.artifact_pull.enabled:
        if profile.data_policy.local_sync == "forbidden":
            return {
                "status": "failed",
                "pulled_paths": [],
                "failures": [
                    {
                        "path": "",
                        "reason": "artifact pull is disabled while local_sync is forbidden",
                    }
                ],
            }
        return {"status": "completed", "pulled_paths": [], "failures": []}
    if (
        profile.data_policy.local_sync == "forbidden"
        and not profile.artifact_pull.allow_patterns
    ):
        return {
            "status": "failed",
            "pulled_paths": [],
            "failures": [
                {
                    "path": "",
                    "reason": (
                        "artifact pull allow_patterns must be configured "
                        "when data_policy.local_sync is forbidden"
                    ),
                }
            ],
        }

    max_bytes = int(max(profile.artifact_pull.max_file_size_mb, 0.0) * 1024 * 1024)
    pulled_paths: list[str] = []
    failures: list[dict[str, str]] = []
    for pattern in profile.artifact_pull.allow_patterns:
        rendered = pattern.format(iteration_id=iteration_id, run_id=run_id)
        for match in _list_remote_matches(
            profile, rendered, timeout_seconds=timeout_seconds
        ):
            rel_path = str(match.get("path", "")).strip()
            if not rel_path:
                continue
            size_bytes = int(match.get("size", 0) or 0)
            reason = _artifact_pull_denial_reason(
                profile,
                rel_path,
                size_bytes=size_bytes,
                max_bytes=max_bytes,
            )
            if reason:
                failures.append({"path": rel_path, "reason": reason})
                continue
            local_target = (repo_root / rel_path).resolve()
            try:
                local_target.relative_to(repo_root.resolve())
            except ValueError:
                failures.append(
                    {"path": rel_path, "reason": "target escapes repo root"}
                )
                continue
            read_proc = run_remote_command(
                profile,
                [profile.python_path, "-c", _remote_file_read_program(), rel_path],
                cwd=profile.remote_repo_root,
                timeout_seconds=timeout_seconds,
                text=False,
            )
            if read_proc.returncode != 0:
                stderr = str(read_proc.stderr or b"").strip()
                failures.append(
                    {
                        "path": rel_path,
                        "reason": f"remote read failed: {stderr or 'unknown error'}",
                    }
                )
                continue
            local_target.parent.mkdir(parents=True, exist_ok=True)
            payload_bytes = bytes(read_proc.stdout or b"")
            if rel_path.endswith("run_manifest.json"):
                _merge_remote_run_manifest(local_target, payload_bytes)
            else:
                local_target.write_bytes(payload_bytes)
            pulled_paths.append(rel_path)
    return {
        "status": "failed" if failures else "completed",
        "pulled_paths": pulled_paths,
        "failures": failures,
    }


def lint_remote_profile(profile: RemoteProfileConfig) -> list[str]:
    issues: list[str] = []
    sample_iteration = "iter_sample"
    sample_run = "run_sample"
    if profile.data_policy.local_sync not in _VALID_LOCAL_SYNC_MODES:
        issues.append(
            f"data_policy.local_sync must be one of {', '.join(sorted(_VALID_LOCAL_SYNC_MODES))}"
        )
    for pattern in profile.artifact_pull.allow_patterns:
        rendered = pattern.format(iteration_id=sample_iteration, run_id=sample_run)
        reason = _artifact_pull_denial_reason(
            profile,
            rendered,
            size_bytes=0,
            max_bytes=int(
                max(profile.artifact_pull.max_file_size_mb, 0.0) * 1024 * 1024
            ),
            check_size=False,
        )
        if reason:
            issues.append(f"allow pattern '{pattern}' is unsafe: {reason}")
    if (
        profile.data_policy.local_sync == "forbidden"
        and not profile.artifact_pull.allow_patterns
    ):
        issues.append(
            "local_sync=forbidden requires at least one artifact_pull allow pattern"
        )
    if not profile.remote_repo_root:
        issues.append("remote_repo_root must be configured")
    if profile.mode != "shared_fs" and not profile.login_host:
        issues.append("login_host must be configured for remote checkout profiles")
    for context, command_text, substitutions in (
        ("bootstrap_command", profile.bootstrap_command, None),
        ("git_sync.fetch_command", profile.git_sync.fetch_command, None),
        (
            "git_sync.checkout_command",
            profile.git_sync.checkout_command,
            {"revision_label": "v0.0.0"},
        ),
        ("submit_command", profile.submit_command, None),
    ):
        if not str(command_text).strip():
            continue
        try:
            _parse_command_template(
                str(command_text),
                context=context,
                substitutions=substitutions,
            )
        except StageCheckError as exc:
            issues.append(str(exc))
    return issues


def _parse_remote_profile(
    name: str, raw_profile: dict[str, Any]
) -> RemoteProfileConfig:
    host_detection_raw = raw_profile.get("host_detection")
    if not isinstance(host_detection_raw, dict):
        host_detection_raw = {}
    git_sync_raw = raw_profile.get("git_sync")
    if not isinstance(git_sync_raw, dict):
        git_sync_raw = {}
    artifact_pull_raw = raw_profile.get("artifact_pull")
    if not isinstance(artifact_pull_raw, dict):
        artifact_pull_raw = {}
    data_policy_raw = raw_profile.get("data_policy")
    if not isinstance(data_policy_raw, dict):
        data_policy_raw = {}
    env_raw = raw_profile.get("env")
    if not isinstance(env_raw, dict):
        env_raw = {}
    cache_vars = env_raw.get("cache_vars")
    if not isinstance(cache_vars, dict):
        cache_vars = {}
    return RemoteProfileConfig(
        name=name,
        mode=normalize_profile_mode(str(raw_profile.get("mode", DEFAULT_PROFILE_MODE))),
        enabled_for_host_modes=_string_tuple(
            raw_profile.get("enabled_for_host_modes"), default=("slurm",)
        ),
        login_host=str(raw_profile.get("login_host", "")).strip(),
        remote_repo_root=str(raw_profile.get("remote_repo_root", "")).strip(),
        bootstrap_command=str(raw_profile.get("bootstrap_command", "")).strip(),
        python_path=str(raw_profile.get("python_path", "python")).strip() or "python",
        submit_command=str(raw_profile.get("submit_command", "sbatch")).strip()
        or "sbatch",
        host_detection=RemoteHostDetectionConfig(
            require_commands=_string_tuple(host_detection_raw.get("require_commands"))
        ),
        git_sync=RemoteGitSyncConfig(
            revision_source=str(git_sync_raw.get("revision_source", "git_tag")).strip()
            or "git_tag",
            require_clean_worktree=_coerce_bool(
                git_sync_raw.get("require_clean_worktree"), default=True
            ),
            fetch_command=str(
                git_sync_raw.get("fetch_command", "git fetch --tags origin")
            ).strip()
            or "git fetch --tags origin",
            checkout_command=str(
                git_sync_raw.get(
                    "checkout_command", "git checkout --force {revision_label}"
                )
            ).strip()
            or "git checkout --force {revision_label}",
        ),
        artifact_pull=RemoteArtifactPullConfig(
            enabled=_coerce_bool(artifact_pull_raw.get("enabled"), default=False),
            allow_patterns=_string_tuple(artifact_pull_raw.get("allow_patterns")),
            max_file_size_mb=_coerce_float(
                artifact_pull_raw.get("max_file_size_mb"), default=50.0
            ),
        ),
        data_policy=RemoteDataPolicyConfig(
            local_sync=str(data_policy_raw.get("local_sync", "allowed")).strip()
            or "allowed",
            deny_patterns=_string_tuple(data_policy_raw.get("deny_patterns")),
        ),
        env=RemoteEnvConfig(
            cache_vars={
                str(key).strip(): str(value).strip()
                for key, value in cache_vars.items()
                if str(key).strip() and str(value).strip()
            }
        ),
        smoke_command=str(raw_profile.get("smoke_command", "")).strip(),
    )


def _string_tuple(value: Any, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if not isinstance(value, list):
        return tuple(default)
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            result.append(text)
    return tuple(result)


def _parse_command_template(
    raw_command: str,
    *,
    context: str,
    substitutions: dict[str, str] | None = None,
) -> list[str]:
    text = str(raw_command or "").strip()
    if not text:
        return []
    if any(fragment in text for fragment in _UNSAFE_COMMAND_FRAGMENTS):
        raise StageCheckError(f"{context} must be a single shell-free command")
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise StageCheckError(f"{context} is not valid shell quoting: {exc}") from exc
    rendered: list[str] = []
    replacements = substitutions or {}
    for token in tokens:
        candidate = token
        for key, value in replacements.items():
            candidate = candidate.replace("{" + key + "}", value)
        if "{" in candidate or "}" in candidate:
            raise StageCheckError(f"{context} contains an unsupported placeholder")
        rendered.append(candidate)
    if not rendered:
        raise StageCheckError(f"{context} must not be empty")
    return rendered


def _command_chain(*argv_groups: list[str]) -> str:
    return " && ".join(shlex.join(group) for group in argv_groups if group)


def _command_segment(
    command: str | list[str],
    *,
    env: dict[str, str],
    allow_compound: bool,
) -> str:
    if isinstance(command, str):
        if allow_compound:
            command_segment = command
        else:
            command_segment = shlex.join(
                _parse_command_template(command, context="remote command")
            )
    else:
        command_segment = shlex.join(command)
    if env:
        env_prefix = shlex.join(
            ["env", *[f"{key}={value}" for key, value in sorted(env.items())]]
        )
        return f"{env_prefix} {command_segment}"
    return command_segment


def _remote_env_map(profile: RemoteProfileConfig) -> dict[str, str]:
    return {
        key: value.replace("{remote_repo_root}", profile.remote_repo_root)
        for key, value in profile.env.cache_vars.items()
    }


def _remote_provenance_text(
    *,
    profile: RemoteProfileConfig,
    cwd: str,
    argv: list[str],
    env: dict[str, str],
) -> str:
    env_prefix = ""
    if env:
        env_prefix = (
            shlex.join(
                ["env", *[f"{key}={value}" for key, value in sorted(env.items())]]
            )
            + " "
        )
    return (
        f"ssh {profile.login_host} "
        f"'(cd {shlex.quote(cwd)} && {env_prefix}{shlex.join(argv)})'"
    )


def _list_remote_matches(
    profile: RemoteProfileConfig, pattern: str, *, timeout_seconds: float
) -> list[dict[str, Any]]:
    code = (
        "import glob, json, os, sys\n"
        "pattern = sys.argv[1]\n"
        "matches = []\n"
        "for candidate in sorted(glob.glob(pattern, recursive=True)):\n"
        "    if not os.path.isfile(candidate):\n"
        "        continue\n"
        "    matches.append({'path': candidate.replace('\\\\', '/'), 'size': os.path.getsize(candidate)})\n"
        "print(json.dumps(matches))\n"
    )
    proc = run_remote_command(
        profile,
        [profile.python_path, "-c", code, pattern],
        cwd=profile.remote_repo_root,
        timeout_seconds=timeout_seconds,
    )
    if proc.returncode != 0:
        stderr = str(proc.stderr or "").strip()
        raise StageCheckError(
            f"remote artifact listing failed for pattern '{pattern}': {stderr or 'unknown error'}"
        )
    try:
        loaded = json.loads(str(proc.stdout or "[]"))
    except Exception as exc:
        raise StageCheckError(
            f"remote artifact listing returned invalid JSON for pattern '{pattern}'"
        ) from exc
    return loaded if isinstance(loaded, list) else []


def _artifact_pull_denial_reason(
    profile: RemoteProfileConfig,
    rel_path: str,
    *,
    size_bytes: int,
    max_bytes: int,
    check_size: bool = True,
) -> str:
    normalized = rel_path.replace("\\", "/")
    for deny_pattern in profile.data_policy.deny_patterns:
        if fnmatch.fnmatch(normalized, deny_pattern):
            return f"matches deny pattern '{deny_pattern}'"
    if check_size and max_bytes > 0 and size_bytes > max_bytes:
        size_mb = size_bytes / (1024 * 1024)
        max_mb = max_bytes / (1024 * 1024)
        return f"file size {size_mb:.2f}MB exceeds cap {max_mb:.2f}MB"
    return ""


def _remote_file_read_program() -> str:
    return (
        "from pathlib import Path\n"
        "import sys\n"
        "sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())\n"
    )


def _merge_remote_run_manifest(local_target: Path, payload_bytes: bytes) -> None:
    try:
        remote_payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        local_target.write_bytes(payload_bytes)
        return
    if not isinstance(remote_payload, dict):
        local_target.write_bytes(payload_bytes)
        return
    local_payload = _load_json_if_exists(local_target)
    if not isinstance(local_payload, dict):
        local_target.write_text(json.dumps(remote_payload, indent=2), encoding="utf-8")
        return
    preserved = {
        "remote_execution": local_payload.get("remote_execution"),
        "workspace_revision": local_payload.get("workspace_revision"),
    }
    merged = dict(remote_payload)
    for key, value in preserved.items():
        if value is not None:
            merged[key] = value
    local_target.write_text(json.dumps(merged, indent=2), encoding="utf-8")

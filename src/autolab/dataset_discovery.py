from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_MEDIA_EXTENSIONS: tuple[str, ...] = (".mp4",)
_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {".git", ".autolab", ".venv", "venv", "__pycache__"}
)


@dataclass
class MediaDiscoveryResult:
    project_roots: list[Path]
    project_root_counts: dict[Path, int]
    project_media_files: list[Path]
    fallback_roots: list[Path]
    fallback_root_counts: dict[Path, int]
    fallback_media_files: list[Path]

    @property
    def media_files(self) -> list[Path]:
        if self.project_media_files:
            return list(self.project_media_files)
        return list(self.fallback_media_files)

    @property
    def used_fallback(self) -> bool:
        return not self.project_media_files and bool(self.fallback_media_files)


def _normalize_exts(extensions: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for ext in extensions:
        candidate = str(ext).strip().lower()
        if not candidate:
            continue
        if not candidate.startswith("."):
            candidate = f".{candidate}"
        if candidate not in normalized:
            normalized.append(candidate)
    if normalized:
        return tuple(normalized)
    return DEFAULT_MEDIA_EXTENSIONS


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path.absolute()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(resolved)
    return deduped


def project_data_root_candidates(
    repo_root: Path,
    *,
    iteration_dir: Path | None = None,
) -> list[Path]:
    candidates: list[Path] = [
        repo_root / "data",
        repo_root / "data" / "curated_yt_drummers",
    ]
    if iteration_dir is not None:
        candidates.append(iteration_dir / "data")
    return _dedupe_paths(candidates)


def fallback_data_root_candidates(repo_root: Path) -> list[Path]:
    parent = repo_root.parent
    return _dedupe_paths(
        [
            parent / "data",
            parent / "datasets",
        ]
    )


def _scan_media_files(root: Path, *, extensions: tuple[str, ...]) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    ext_set = set(extensions)
    matches: list[Path] = []
    for raw_dir, dir_names, file_names in os.walk(root):
        dir_names[:] = sorted(
            name for name in dir_names if name and name not in _SKIP_DIR_NAMES
        )
        for file_name in sorted(file_names):
            suffix = Path(file_name).suffix.lower()
            if suffix not in ext_set:
                continue
            candidate = Path(raw_dir) / file_name
            if not candidate.is_file():
                continue
            try:
                matches.append(candidate.resolve())
            except Exception:
                matches.append(candidate.absolute())
    return _dedupe_paths(matches)


def discover_media_inputs(
    repo_root: Path,
    *,
    iteration_dir: Path | None = None,
    extensions: Iterable[str] = DEFAULT_MEDIA_EXTENSIONS,
    allow_outside_repo_fallback: bool = True,
) -> MediaDiscoveryResult:
    normalized_exts = _normalize_exts(extensions)
    project_roots = project_data_root_candidates(repo_root, iteration_dir=iteration_dir)
    project_root_counts: dict[Path, int] = {}
    project_media_files: list[Path] = []
    for root in project_roots:
        files = _scan_media_files(root, extensions=normalized_exts)
        project_root_counts[root] = len(files)
        if files:
            project_media_files.extend(files)
    project_media_files = _dedupe_paths(project_media_files)

    fallback_roots: list[Path] = []
    fallback_root_counts: dict[Path, int] = {}
    fallback_media_files: list[Path] = []
    if allow_outside_repo_fallback and not project_media_files:
        fallback_roots = fallback_data_root_candidates(repo_root)
        for root in fallback_roots:
            files = _scan_media_files(root, extensions=normalized_exts)
            fallback_root_counts[root] = len(files)
            if files:
                fallback_media_files.extend(files)
        fallback_media_files = _dedupe_paths(fallback_media_files)

    return MediaDiscoveryResult(
        project_roots=project_roots,
        project_root_counts=project_root_counts,
        project_media_files=project_media_files,
        fallback_roots=fallback_roots,
        fallback_root_counts=fallback_root_counts,
        fallback_media_files=fallback_media_files,
    )


_QUOTED_PATH_RE = re.compile(
    r"""['"](?P<path>[^'"]+\.(?:mp4))['"]""",
    flags=re.IGNORECASE,
)
_ABSOLUTE_MEDIA_RE = re.compile(
    r"(?P<path>/[^|,\n\r\t ]+\.(?:mp4))",
    flags=re.IGNORECASE,
)


def _extract_media_path_from_segment_line(line: str) -> str:
    stripped = str(line).strip()
    if not stripped or stripped.startswith("#"):
        return ""
    if stripped.startswith("- "):
        stripped = stripped[2:].strip()

    quoted_match = _QUOTED_PATH_RE.search(stripped)
    if quoted_match:
        return str(quoted_match.group("path")).strip()

    absolute_match = _ABSOLUTE_MEDIA_RE.search(stripped)
    if absolute_match:
        return str(absolute_match.group("path")).strip()

    token = stripped.split("|", 1)[0].split(",", 1)[0].strip()
    if not token:
        return ""
    return token.strip("'\"")


def parse_runnable_media_entries(
    segment_list_path: Path,
    *,
    extensions: Iterable[str] = DEFAULT_MEDIA_EXTENSIONS,
) -> list[Path]:
    if not segment_list_path.exists():
        return []

    normalized_exts = set(_normalize_exts(extensions))
    runnable: list[Path] = []
    seen: set[str] = set()
    for raw_line in segment_list_path.read_text(encoding="utf-8").splitlines():
        token = _extract_media_path_from_segment_line(raw_line)
        if not token:
            continue
        candidate = Path(token).expanduser()
        if not candidate.is_absolute():
            candidate = (segment_list_path.parent / candidate).resolve()
        else:
            try:
                candidate = candidate.resolve()
            except Exception:
                candidate = candidate.absolute()
        if candidate.suffix.lower() not in normalized_exts:
            continue
        if not candidate.is_file():
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        runnable.append(candidate)
    return runnable


def populate_segment_list_from_media(
    segment_list_path: Path,
    media_files: Iterable[Path],
    *,
    max_entries: int = 8,
    extensions: Iterable[str] = DEFAULT_MEDIA_EXTENSIONS,
) -> tuple[list[Path], bool]:
    limit = int(max_entries) if int(max_entries) > 0 else 8
    normalized_exts = set(_normalize_exts(extensions))
    selected: list[Path] = []
    seen: set[str] = set()

    for raw in media_files:
        candidate = Path(raw).expanduser()
        try:
            candidate = candidate.resolve()
        except Exception:
            candidate = candidate.absolute()
        if candidate.suffix.lower() not in normalized_exts:
            continue
        if not candidate.is_file():
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        selected.append(candidate)
        if len(selected) >= limit:
            break

    rendered = "\n".join(str(path) for path in selected)
    if rendered:
        rendered = f"{rendered}\n"
    segment_list_path.parent.mkdir(parents=True, exist_ok=True)
    previous = (
        segment_list_path.read_text(encoding="utf-8")
        if segment_list_path.exists()
        else ""
    )
    changed = previous != rendered
    if changed:
        segment_list_path.write_text(rendered, encoding="utf-8")
    return (selected, changed)


def summarize_root_counts(root_counts: dict[Path, int]) -> str:
    if not root_counts:
        return "none"
    parts = [f"{root}={int(count)}" for root, count in root_counts.items()]
    return ", ".join(parts) if parts else "none"

from __future__ import annotations

from pathlib import Path

from autolab.dataset_discovery import (
    discover_media_inputs,
    parse_runnable_media_entries,
    populate_segment_list_from_media,
)


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")


def test_discover_media_inputs_prefers_project_roots(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    project_mp4 = repo / "data" / "curated_yt_drummers" / "clip01.mp4"
    _touch(project_mp4)

    fallback_mp4 = tmp_path / "data" / "outside.mp4"
    _touch(fallback_mp4)

    result = discover_media_inputs(repo)

    assert result.project_media_files
    assert str(project_mp4.resolve()) in {str(path) for path in result.media_files}
    assert not result.used_fallback
    assert all(str(path).startswith(str(repo.resolve())) for path in result.media_files)


def test_parse_runnable_media_entries_accepts_pipe_suffix_lines(tmp_path: Path) -> None:
    media_path = tmp_path / "videos" / "sample.mp4"
    _touch(media_path)
    segment_list = tmp_path / "segment_list.txt"
    segment_list.write_text(
        f"{media_path.resolve()}|start=0|end=10\n# comment\n", encoding="utf-8"
    )

    entries = parse_runnable_media_entries(segment_list)
    assert entries == [media_path.resolve()]


def test_populate_segment_list_from_media_writes_absolute_paths(tmp_path: Path) -> None:
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    _touch(first)
    _touch(second)
    segment_list = tmp_path / "data" / "segment_list.txt"

    selected, changed = populate_segment_list_from_media(
        segment_list,
        [first, second],
        max_entries=1,
    )

    assert changed is True
    assert selected == [first.resolve()]
    assert segment_list.read_text(encoding="utf-8") == f"{first.resolve()}\n"

"""
The duplicate-file finder.

find_duplicates is deliberately two-phase — bucket by size, compare a 64 KiB
head, then confirm with a full hash — because hashing a whole home directory
is the one way this feature could hang the app. These tests pin the parts that
make it safe and honest:

* identical files group, unique files never do;
* a file that shares a size and a first 64 KiB but differs later is NOT a
  duplicate (the confirmation pass actually runs);
* hard links to the same inode are one file, not two;
* empty files, sub-threshold files and symlinks are ignored;
* the oldest copy is the one kept;
* the time budget yields a flagged partial result rather than a hang.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mac_cleaner import maintenance


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_identical_files_are_grouped_and_unique_ones_are_not(tmp_path):
    body = b"repeat me " * 500
    a = _write(tmp_path / "a.txt", body)
    b = _write(tmp_path / "nested" / "b.txt", body)
    _write(tmp_path / "unique.txt", b"something else entirely")
    report = maintenance.find_duplicates(roots=[str(tmp_path)])
    assert report["matched_groups"] == 1
    group = report["groups"][0]
    assert group["count"] == 2
    assert {f["path"] for f in group["files"]} == {str(a), str(b)}
    assert group["wasted_bytes"] == len(body)


def test_same_head_different_tail_is_not_a_duplicate(tmp_path):
    head = b"H" * 70000
    _write(tmp_path / "one.bin", head + b"tail-A")
    _write(tmp_path / "two.bin", head + b"tail-B")
    report = maintenance.find_duplicates(roots=[str(tmp_path)])
    assert report["matched_groups"] == 0, "the head hash must not be the verdict"


def test_hard_links_are_not_reported_as_their_own_duplicate(tmp_path):
    body = b"linked contents " * 300
    original = _write(tmp_path / "original.dat", body)
    os.link(original, tmp_path / "link.dat")
    report = maintenance.find_duplicates(roots=[str(tmp_path)])
    assert report["matched_groups"] == 0


def test_the_oldest_copy_is_the_one_kept(tmp_path):
    body = b"same bytes " * 400
    old = _write(tmp_path / "old.txt", body)
    new = _write(tmp_path / "new.txt", body)
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    group = maintenance.find_duplicates(roots=[str(tmp_path)])["groups"][0]
    assert group["keep"] == str(old)
    kept = [f for f in group["files"] if f["is_oldest"]]
    assert len(kept) == 1 and kept[0]["path"] == str(old)


def test_empty_and_tiny_files_are_ignored(tmp_path):
    _write(tmp_path / "empty-a", b"")
    _write(tmp_path / "empty-b", b"")
    _write(tmp_path / "small-a", b"hi")
    _write(tmp_path / "small-b", b"hi")
    # min_size_kb=1 => only files >= 1024 bytes are candidates.
    report = maintenance.find_duplicates(min_size_kb=1, roots=[str(tmp_path)])
    assert report["matched_groups"] == 0
    assert report["files_scanned"] == 4


def test_symlinks_are_ignored(tmp_path):
    body = b"real contents " * 500
    target = _write(tmp_path / "real.txt", body)
    os.symlink(target, tmp_path / "ghost.txt")
    report = maintenance.find_duplicates(roots=[str(tmp_path)])
    assert report["matched_groups"] == 0


def test_limit_caps_groups_shown_but_not_the_totals(tmp_path):
    for size, name in ((4096, "big"), (3072, "mid"), (2048, "small")):
        for copy in ("1", "2"):
            _write(tmp_path / f"{name}-{copy}.bin", bytes(size))
    report = maintenance.find_duplicates(limit=2, roots=[str(tmp_path)])
    assert report["shown_groups"] == 2
    assert report["matched_groups"] == 3
    assert report["reclaimable_bytes"] == 4096 + 3072 + 2048
    # Sorted biggest-reclaimable first.
    assert report["groups"][0]["size_bytes"] == 4096


def test_reclaimable_counts_all_but_one_copy_per_group(tmp_path):
    body = b"x" * 5000
    for name in ("a", "b", "c"):
        _write(tmp_path / f"{name}.bin", body)
    report = maintenance.find_duplicates(roots=[str(tmp_path)])
    assert report["duplicate_files"] == 3
    assert report["reclaimable_bytes"] == 5000 * 2


def test_the_time_budget_is_flagged_not_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "_DUP_TIME_BUDGET", -1.0)
    _write(tmp_path / "a.txt", b"data " * 400)
    report = maintenance.find_duplicates(roots=[str(tmp_path)])
    assert report["partial_scan"] is True


def test_a_file_that_cannot_be_read_is_skipped_not_fatal(tmp_path, monkeypatch):
    body = b"unreadable " * 300
    a = _write(tmp_path / "a.bin", body)
    b = _write(tmp_path / "b.bin", body)
    real_partial = maintenance._partial_digest

    def flaky(path: str):
        if path == str(a):
            return None
        return real_partial(path)

    monkeypatch.setattr(maintenance, "_partial_digest", flaky)
    report = maintenance.find_duplicates(roots=[str(tmp_path)])
    # Only b survived the head pass, so no group — and nothing raised.
    assert report["matched_groups"] == 0

    # Same for the confirmation pass.
    monkeypatch.setattr(maintenance, "_partial_digest", real_partial)
    monkeypatch.setattr(maintenance, "_full_digest", lambda path: None)
    assert maintenance.find_duplicates(roots=[str(tmp_path)])["matched_groups"] == 0


def test_the_default_root_is_the_home_directory_with_library_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    body = b"identical " * 300
    _write(tmp_path / "top-a.txt", body)
    _write(tmp_path / "top-b.txt", body)
    # A copy inside the top-level Library must be pruned from the home scan.
    _write(tmp_path / "Library" / "copy.txt", body)
    report = maintenance.find_duplicates(min_size_kb=1)
    group = report["groups"][0]
    assert group["count"] == 2
    assert all("Library" not in f["path"] for f in group["files"])


def test_an_explicit_root_is_honoured_even_for_library(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    body = b"identical " * 300
    _write(tmp_path / "Library" / "one.txt", body)
    _write(tmp_path / "Library" / "two.txt", body)
    report = maintenance.find_duplicates(roots=[str(tmp_path / "Library")])
    assert report["matched_groups"] == 1


def test_the_note_says_nothing_was_deleted(tmp_path):
    report = maintenance.find_duplicates(roots=[str(tmp_path)])
    assert "nothing was deleted" in report["note"].lower()


@pytest.mark.parametrize("min_size_kb", [0, -5])
def test_a_nonsense_minimum_size_falls_back_to_one_kb(tmp_path, min_size_kb):
    body = b"tiny " * 20  # 100 bytes
    _write(tmp_path / "a.txt", body)
    _write(tmp_path / "b.txt", body)
    report = maintenance.find_duplicates(min_size_kb=min_size_kb, roots=[str(tmp_path)])
    assert report["min_size_kb"] == min_size_kb  # echoed as asked
    assert report["matched_groups"] == 0  # but the floor is still 1 KB

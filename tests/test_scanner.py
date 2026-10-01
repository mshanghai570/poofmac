"""
The scanners that build the cleanup plan.

scanner.py proposes things to delete, so its bugs are the kind that end with
someone regretting a click: a category that reports a size it never measured,
a path that is not what the description says, a risk level that drifts.

Every test here runs against a fake home directory (patched onto
Path.home) with `du` and `find` stubbed, so the plan is built from known
inputs and nothing on the real machine is measured or walked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mac_cleaner import scanner

MB = 1024**2


# ── Helpers: a fake home and a fake du/find ───────────────────────────────────

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(scanner.Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def du_sizes(monkeypatch):
    """Size table for _du/_du_batch: {path-as-string: bytes}."""
    table: dict[str, int] = {}

    monkeypatch.setattr(
        scanner, "_du", lambda path, timeout=30: table.get(str(path), 0)
    )
    monkeypatch.setattr(
        scanner, "_du_batch", lambda paths, timeout=60: {p: table.get(str(p), 0) for p in paths}
    )
    return table


def _make(base: Path, *parts: str) -> Path:
    path = base.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── _du_batch ─────────────────────────────────────────────────────────────────

def test_du_batch_returns_a_size_per_path(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = "512\t/one\n2048\t/two\n"

    monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: _Proc())
    sizes = scanner._du_batch(["/one", "/two"])
    assert sizes == {"/one": 512 * 1024, "/two": 2048 * 1024}


def test_du_batch_of_nothing_is_free(monkeypatch):
    monkeypatch.setattr(
        scanner.subprocess, "run", lambda *a, **k: pytest.fail("should not shell out")
    )
    assert scanner._du_batch([]) == {}


def test_du_batch_skips_garbage_lines(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = "512\t/one\ndu: cannot access '/two'\nnot-a-number\t/three\n"

    monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: _Proc())
    assert scanner._du_batch(["/one", "/two", "/three"]) == {"/one": 512 * 1024}


def test_du_batch_chunks_long_lists(monkeypatch):
    """du fails with E2BIG when handed hundreds of paths in one call."""
    lengths = []

    class _Proc:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(cmd, *a, **k):
        paths = cmd[2:] if cmd[1] == "-sk" else []
        lengths.append(len(paths))
        return _Proc("".join(f"1\t{p}\n" for p in paths))

    monkeypatch.setattr(scanner.subprocess, "run", fake_run)
    scanner._du_batch([f"/p{i}" for i in range(120)])
    assert all(n <= 50 for n in lengths), lengths
    assert sum(lengths) == 120


def test_du_reports_zero_when_the_command_fails(monkeypatch):
    class _Proc:
        returncode = 1
        stdout = "du: /nope: No such file or directory"

    monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: _Proc())
    assert scanner._du("/nope") == 0


def test_du_survives_a_timeout(monkeypatch):
    def boom(*a, **k):
        raise scanner.subprocess.TimeoutExpired("du", 30)

    monkeypatch.setattr(scanner.subprocess, "run", boom)
    assert scanner._du("/slow") == 0


# ── Application caches ────────────────────────────────────────────────────────

def test_caches_are_listed_biggest_first(fake_home, du_sizes):
    caches = _make(fake_home, "Library", "Caches")
    for name in ("Slack", "Tiny", "Chrome"):
        _make(caches, name)
    du_sizes[str(caches / "Slack")] = 900 * MB
    du_sizes[str(caches / "Tiny")] = 2 * MB
    du_sizes[str(caches / "Chrome")] = 300 * MB

    results = scanner.scan_user_caches()
    assert len(results) == 1
    result = results[0]
    assert result.safe_to_delete and result.risk_level == "SAFE"
    assert [i["name"] for i in result.items] == ["Slack", "Chrome", "Tiny"]
    assert result.size_bytes == 1202 * MB
    assert "3 apps" in result.description


def test_hidden_entries_are_ignored(fake_home, du_sizes):
    caches = _make(fake_home, "Library", "Caches")
    _make(caches, ".DS_Store")
    _make(caches, "Visible")
    du_sizes[str(caches / "Visible")] = 10 * MB
    result = scanner.scan_user_caches()[0]
    assert [i["name"] for i in result.items] == ["Visible"]


def test_zero_sized_cache_entries_are_dropped(fake_home, du_sizes):
    """A du that returns 0 usually means 'no permission', not 'empty'."""
    caches = _make(fake_home, "Library", "Caches")
    _make(caches, "Readable")
    _make(caches, "Denied")
    du_sizes[str(caches / "Readable")] = 10 * MB
    result = scanner.scan_user_caches()[0]
    assert [i["name"] for i in result.items] == ["Readable"]


def test_no_cache_directory_means_no_result(fake_home, du_sizes):
    assert scanner.scan_user_caches() == []


def test_an_empty_cache_directory_reports_nothing(fake_home, du_sizes):
    _make(fake_home, "Library", "Caches")
    assert scanner.scan_user_caches() == []


# ── System logs ───────────────────────────────────────────────────────────────

def test_logs_are_reported_with_their_real_size(fake_home, du_sizes):
    logs = _make(fake_home, "Library", "Logs")
    du_sizes[str(logs)] = 250 * MB
    result = scanner.scan_system_logs()[0]
    assert result.size_bytes == 250 * MB
    assert result.path == str(logs)
    assert result.risk_level in {"SAFE", "CAUTION"}


def test_an_empty_logs_directory_is_not_reported(fake_home, du_sizes):
    """A zero size means 'could not measure' or 'empty' — never a cleanup row."""
    logs = _make(fake_home, "Library", "Logs")
    du_sizes[str(logs)] = 0
    assert scanner.scan_system_logs() == []


def test_missing_logs_directory(fake_home, du_sizes):
    assert scanner.scan_system_logs() == []


# ── Xcode ─────────────────────────────────────────────────────────────────────

def test_xcode_artifacts_are_separated_from_caches(fake_home, du_sizes):
    derived = _make(fake_home, "Library", "Developer", "Xcode", "DerivedData")
    du_sizes[str(derived)] = 5 * 1024 * MB
    results = scanner.scan_xcode_artifacts()
    assert results, "5 GB of DerivedData should not go unreported"
    assert results[0].category == "Xcode DerivedData"
    assert results[0].path == str(derived)
    assert all(r.risk_level in {"SAFE", "CAUTION"} for r in results)


def test_unmeasurable_xcode_paths_are_skipped(fake_home, du_sizes):
    _make(fake_home, "Library", "Developer", "Xcode", "DerivedData")
    du_sizes[str(fake_home / "Library" / "Developer" / "Xcode" / "DerivedData")] = 0
    assert scanner.scan_xcode_artifacts() == []


def test_no_xcode_install_means_no_results(fake_home, du_sizes):
    assert scanner.scan_xcode_artifacts() == []


# ── Dev artifacts ─────────────────────────────────────────────────────────────

def _fake_find(monkeypatch, results: dict[str, list[str]]):
    """Answer `find <root> -name <artifact> …` from a table.

    Any other command (brew, docker, du) gets an empty successful result so a
    single stub can stand in for every shell-out in the module.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        if "-name" not in cmd:
            return type("P", (), {"returncode": 0, "stdout": ""})()
        artifact = cmd[cmd.index("-name") + 1]
        root = cmd[1]
        paths = results.get(f"{root}:{artifact}", [])
        return type("P", (), {"returncode": 0, "stdout": "".join(f"{p}\n" for p in paths)})()

    monkeypatch.setattr(scanner.subprocess, "run", fake_run)
    return calls


def test_node_modules_and_venvs_are_found_and_labelled(fake_home, du_sizes, monkeypatch):
    project = _make(fake_home, "Code", "app")
    node = _make(project, "node_modules")
    venv = _make(project, ".venv")
    home_str = str(fake_home)
    code_str = str(fake_home / "Code")
    _fake_find(
        monkeypatch,
        {
            f"{home_str}:node_modules": [str(node)],
            f"{code_str}:node_modules": [str(node)],  # found twice — must dedupe
            f"{code_str}:.venv": [str(venv)],
        },
    )
    du_sizes[str(node)] = 400 * MB
    du_sizes[str(venv)] = 90 * MB

    results = scanner.scan_dev_artifacts()
    assert results
    names = {i["name"] for r in results for i in r.items}
    assert names == {"node_modules", ".venv"}, "duplicates must be collapsed"
    sizes = {i["name"]: i["size_bytes"] for r in results for i in r.items}
    assert sizes["node_modules"] == 400 * MB


def test_artifact_risk_levels_separate_caches_from_build_output(
    fake_home, du_sizes, monkeypatch
):
    project = _make(fake_home, "Code", "app")
    dist = _make(project, "dist")
    target = _make(project, "target")
    du_sizes[str(dist)] = 300 * MB
    du_sizes[str(target)] = 700 * MB
    _fake_find(
        monkeypatch,
        {
            f"{str(fake_home)}:dist": [str(dist)],
            f"{str(fake_home)}:target": [str(target)],
        },
    )
    by_name = {i["name"]: i for r in scanner.scan_dev_artifacts() for i in r.items}
    assert by_name["dist"]["risk_level"] == "CAUTION", "build output needs review"
    assert by_name["target"]["risk_level"] == "CAUTION"
    assert "cargo" in by_name["target"]["note"]
    assert scanner.scan_dev_artifacts()[0].safe_to_delete is False


def test_nothing_found_means_no_category(fake_home, du_sizes, monkeypatch):
    _fake_find(monkeypatch, {})
    assert scanner.scan_dev_artifacts() == []


def test_a_missing_find_binary_does_not_crash_the_scan(fake_home, du_sizes, monkeypatch):
    def boom(cmd, *a, **k):
        raise FileNotFoundError("find")

    monkeypatch.setattr(scanner.subprocess, "run", boom)
    assert scanner.scan_dev_artifacts() == []


def test_find_timeouts_are_survived(fake_home, du_sizes, monkeypatch):
    def slow(cmd, *a, **k):
        raise scanner.subprocess.TimeoutExpired("find", 15)

    monkeypatch.setattr(scanner.subprocess, "run", slow)
    assert scanner.scan_dev_artifacts() == []


# ── Homebrew, Trash, Downloads ────────────────────────────────────────────────

def test_homebrew_cache_is_reported_when_present(fake_home, du_sizes, monkeypatch):
    brew = _make(fake_home, "Library", "Caches", "Homebrew")
    du_sizes[str(brew)] = 1200 * MB
    monkeypatch.setattr(
        scanner.subprocess,
        "run",
        lambda *a, **k: type("P", (), {"returncode": 0, "stdout": f"{brew}\n"})(),
    )
    results = scanner.scan_homebrew_cache()
    assert results and results[0].size_bytes == 1200 * MB
    assert results[0].risk_level == "SAFE"
    assert results[0].path == str(brew)


def test_no_homebrew_installed_means_no_row(fake_home, du_sizes, monkeypatch):
    monkeypatch.setattr(
        scanner.subprocess,
        "run",
        lambda *a, **k: type("P", (), {"returncode": 1, "stdout": ""})(),
    )
    assert scanner.scan_homebrew_cache() == []


def test_trash_is_measured_not_emptied(fake_home, du_sizes):
    trash = _make(fake_home, ".Trash")
    du_sizes[str(trash)] = 1900 * MB
    result = scanner.scan_trash()[0]
    assert result.size_bytes == 1900 * MB
    assert "empty the Trash" in result.description.lower() or "trash" in result.description.lower()


def test_downloads_need_a_size_threshold_and_manual_review(
    fake_home, du_sizes, tmp_path
):
    downloads = _make(fake_home, "Downloads")
    big = downloads / "big.dmg"
    big.write_bytes(b"x" * 60 * MB)
    small = downloads / "small.txt"
    small.write_bytes(b"x")
    (downloads / "subdir").mkdir()

    results = scanner.scan_downloads(min_size_mb=50)
    assert results
    assert all(r.risk_level == "CAUTION" for r in results), "Downloads is never auto-safe"
    for result in results:
        for item in result.items:
            assert item["size_bytes"] >= 50 * MB
            assert item["path"].endswith(".dmg") or item["path"] == str(big)
    assert small.name not in {i["name"] for r in results for i in r.items}


def test_a_small_downloads_folder_reports_nothing(fake_home, du_sizes):
    _make(fake_home, "Downloads")
    assert scanner.scan_downloads(min_size_mb=50) == []


# ── The full scan ─────────────────────────────────────────────────────────────

def test_run_full_scan_reports_each_category(fake_home, du_sizes, monkeypatch):
    caches = _make(fake_home, "Library", "Caches")
    _make(caches, "App")
    du_sizes[str(caches / "App")] = 50 * MB
    du_sizes[str(caches)] = 50 * MB
    logs = _make(fake_home, "Library", "Logs")
    du_sizes[str(logs)] = 5 * MB
    _fake_find(monkeypatch, {})

    report = scanner.run_full_scan()
    assert report["disk"], "the scan must open with the disk state"
    summary = report["summary"]
    assert summary["grand_total_bytes"] >= 55 * MB
    assert summary["categories_found"] == len(report["results"])
    assert (
        summary["safe_recoverable_bytes"] + summary["caution_review_bytes"]
        == summary["grand_total_bytes"]
    )
    categories = {r["category"] for r in report["results"]}
    assert categories, "at least one category should be reported"
    for result in report["results"]:
        assert result["risk_level"] in {"SAFE", "CAUTION", "SKIP"}
        assert isinstance(result["size_bytes"], int)


def test_run_full_scan_on_an_empty_mac_still_answers(fake_home, du_sizes, monkeypatch):
    _fake_find(monkeypatch, {})
    report = scanner.run_full_scan()
    assert report["results"] == []
    assert report["summary"]["grand_total_bytes"] == 0
    assert report["summary"]["categories_found"] == 0

"""
Parsing the output of the read-only system probes.

maintenance.py is a thin layer over lsappinfo / ps / vm_stat / tmutil, and
the layer is where the bugs live: a column that shifts, a field that goes
missing, a line format that changes between macOS releases. Each test feeds
real-shaped command output through the stubbed ``_run`` helper and checks
what the agent and the dialog would show.

Nothing here shells out: ``_run`` is the single place maintenance.py talks to
the system, and it is replaced wholesale.
"""

from __future__ import annotations

import time

import pytest

from mac_cleaner import maintenance


@pytest.fixture
def fake_run(monkeypatch):
    """Answer commands from a dict of {program: (ok, stdout)} and record calls.

    A program may map to a list of answers to serve repeat calls in order
    (heavy_consumers calls ps twice, with different formats).
    """
    answers: dict[str, object] = {}
    calls: list[list[str]] = []
    served: dict[str, int] = {}

    def runner(cmd, timeout=10.0):
        calls.append(list(cmd))
        if not cmd or cmd[0] not in answers:
            return False, ""
        answer = answers[cmd[0]]
        if isinstance(answer, list):
            index = served.get(cmd[0], 0)
            served[cmd[0]] = index + 1
            return answer[min(index, len(answer) - 1)]
        return answer

    monkeypatch.setattr(maintenance, "_run", runner)
    runner.answers = answers
    runner.calls = calls
    return runner


# Real `lsappinfo list` output: the quoted name comes first on the indexed
# line, and detail lines are indented underneath it.
LSAPPINFO = """\
 1) "loginwindow" ASN:0x0-0x5005: \n\
    bundleID="com.apple.loginwindow"\n\
    pid = 176 type="UIElement" flavor=3 Arch=x86_64 \n\
\n\
 2) "Safari" ASN:0x0-0x8008: \n\
    bundleID="com.apple.Safari"\n\
    pid = 201 type="Foreground" flavor=3 Arch=x86_64 \n\
\n\
 3) "Dock" ASN:0x0-0x8009: \n\
    bundleID="com.apple.dock"\n\
    pid = 305 type="UIElement" flavor=3 Arch=x86_64 \n\
\n\
 4) "ViewBridgeAuxiliary" ASN:0x0-0x800a: \n\
    bundleID="com.apple.ViewBridgeAuxiliary"\n\
    pid = 411 type="BackgroundOnly" flavor=2 Arch=x86_64 \n\
\n\
 5) "NoPid" ASN:0x0-0x800b: \n\
    bundleID="com.example.nopid"\n\
    type="Foreground" flavor=3 \n\
"""


# ── _foreground_apps ──────────────────────────────────────────────────────────

def test_foreground_apps_are_parsed(fake_run):
    fake_run.answers["lsappinfo"] = (True, LSAPPINFO)
    apps = maintenance._foreground_apps()
    assert {a["name"] for a in apps} == {"loginwindow", "Safari", "Dock"}
    safari = next(a for a in apps if a["name"] == "Safari")
    assert safari["pid"] == 201
    assert safari["bundle_id"] == "com.apple.Safari"
    assert isinstance(safari["pid"], int)


def test_the_first_app_in_the_list_is_not_dropped(fake_run):
    """Regression: the split needed a newline before "1)", so app #1 vanished."""
    fake_run.answers["lsappinfo"] = (True, LSAPPINFO)
    assert "loginwindow" in {a["name"] for a in maintenance._foreground_apps()}


def test_uielement_apps_are_listed_but_background_helpers_are_not(fake_run):
    """Dock registers as UIElement; XPC helpers must not show up as apps."""
    fake_run.answers["lsappinfo"] = (True, LSAPPINFO)
    names = {a["name"] for a in maintenance._foreground_apps()}
    assert "Dock" in names
    assert "ViewBridgeAuxiliary" not in names


def test_a_malformed_entry_is_skipped_not_fatal(fake_run):
    """The "NoPid" block has no pid — it must be skipped, not crash the parse."""
    fake_run.answers["lsappinfo"] = (True, LSAPPINFO)
    names = {a["name"] for a in maintenance._foreground_apps()}
    assert "NoPid" not in names
    assert all(a["pid"] for a in maintenance._foreground_apps())


def test_an_entry_without_a_bundle_id_still_appears(fake_run):
    """force_quit_app lowercases bundle_id — an empty one must not crash it."""
    fake_run.answers["lsappinfo"] = (
        True,
        ' 1) "Thing" ASN:0x0-0x1: \n    pid = 42 type="Foreground" flavor=3 \n',
    )
    apps = maintenance._foreground_apps()
    assert apps == [{"name": "Thing", "pid": 42, "bundle_id": ""}]


def test_lsappinfo_failing_gives_an_empty_list(fake_run):
    fake_run.answers["lsappinfo"] = (False, "command not found")
    assert maintenance._foreground_apps() == []


# ── hung_applications ─────────────────────────────────────────────────────────

def test_apps_in_uninterruptible_wait_are_suspected(fake_run, tmp_path, monkeypatch):
    fake_run.answers["lsappinfo"] = (True, LSAPPINFO)
    fake_run.answers["ps"] = (
        True,
        "176 S\n201 S\n305 U\n",  # "305 U" is the stuck-in-a-syscall signature
    )
    reports = tmp_path / "Library" / "Logs" / "DiagnosticReports"
    reports.mkdir(parents=True)
    monkeypatch.setattr(maintenance.Path, "home", classmethod(lambda cls: tmp_path))

    result = maintenance.hung_applications()
    assert result["count"] == 1
    assert result["suspected_hung"][0]["name"] == "Dock"
    assert "U" in result["suspected_hung"][0]["state"]
    assert result["gui_apps"]


def test_one_batched_ps_covers_every_app(fake_run, tmp_path, monkeypatch):
    """Regression: a ps per app took ~20s and made the dialog feel hung."""
    fake_run.answers["lsappinfo"] = (True, LSAPPINFO)
    fake_run.answers["ps"] = (True, "176 S\n201 S\n305 S\n")
    monkeypatch.setattr(maintenance.Path, "home", classmethod(lambda cls: tmp_path))
    maintenance.hung_applications()
    ps_calls = [c for c in fake_run.calls if c[0] == "ps"]
    assert len(ps_calls) == 1, f"expected one batched ps, got {len(ps_calls)}"
    assert "176,201,305" in ps_calls[0], ps_calls[0]


def test_garbage_from_ps_is_ignored(fake_run, tmp_path, monkeypatch):
    fake_run.answers["lsappinfo"] = (True, LSAPPINFO)
    fake_run.answers["ps"] = (True, "not a pid line\n\n201 S\n305 S\n")
    monkeypatch.setattr(maintenance.Path, "home", classmethod(lambda cls: tmp_path))
    assert maintenance.hung_applications()["count"] == 0


def test_recorded_freezes_are_reported_with_their_age(fake_run, tmp_path, monkeypatch):
    fake_run.answers["lsappinfo"] = (True, LSAPPINFO)
    fake_run.answers["ps"] = (True, "176 S\n201 S\n305 S\n")
    reports = tmp_path / "Library" / "Logs" / "DiagnosticReports"
    reports.mkdir(parents=True)
    stale = reports / "Safari-2026-09-28-030000.hang.ips"
    stale.write_text("{}")
    import os

    old = time.time() - 3 * 3600
    os.utime(stale, (old, old))
    monkeypatch.setattr(maintenance.Path, "home", classmethod(lambda cls: tmp_path))

    hangs = maintenance.hung_applications()["hang_reports"]
    assert len(hangs) == 1
    assert hangs[0]["app"] == "Safari", "the date suffix must be stripped"
    assert 2.9 < hangs[0]["hours_ago"] < 3.1


def test_a_hyphenated_app_name_survives_the_date_strip(fake_run, tmp_path, monkeypatch):
    """'Minecraft-2' must not come back as 'Minecraft'."""
    fake_run.answers["lsappinfo"] = (True, LSAPPINFO)
    fake_run.answers["ps"] = (True, "176 S\n")
    reports = tmp_path / "Library" / "Logs" / "DiagnosticReports"
    reports.mkdir(parents=True)
    (reports / "Minecraft-2-2026-09-28-030000.hang.ips").write_text("{}")
    monkeypatch.setattr(maintenance.Path, "home", classmethod(lambda cls: tmp_path))
    assert maintenance.hung_applications()["hang_reports"][0]["app"] == "Minecraft-2"


def test_hang_reports_are_capped(fake_run, tmp_path, monkeypatch):
    fake_run.answers["lsappinfo"] = (True, LSAPPINFO)
    fake_run.answers["ps"] = (True, "176 S\n201 S\n")
    reports = tmp_path / "Library" / "Logs" / "DiagnosticReports"
    reports.mkdir(parents=True)
    for i in range(15):
        (reports / f"App{i}-2026-09-28-03000{i % 10}.hang.ips").write_text("{}")
    monkeypatch.setattr(maintenance.Path, "home", classmethod(lambda cls: tmp_path))
    assert len(maintenance.hung_applications()["hang_reports"]) == 10


def test_the_note_tells_the_agent_to_ask_before_killing(fake_run, tmp_path, monkeypatch):
    fake_run.answers["lsappinfo"] = (True, LSAPPINFO)
    fake_run.answers["ps"] = (True, "176 S\n201 S\n")
    monkeypatch.setattr(maintenance.Path, "home", classmethod(lambda cls: tmp_path))
    note = maintenance.hung_applications()["note"].lower()
    assert "ask" in note and "lost" in note


# ── heavy_consumers ───────────────────────────────────────────────────────────

PS_CPU = """\
%CPU %MEM PID COMMAND
12.5  3.1  201 /Applications/Safari.app/Contents/MacOS/Safari
 0.0  0.1  305 /System/Applications/Dock.app/Contents/MacOS/Dock
 9.9  8.0  411 /Applications/Chat.app/Contents/MacOS/Chat
not-a-number 1 1 /bin/whatever
"""

PS_RSS = """\
RSS   PID COMMAND
1048576  201 /Applications/Safari.app/Contents/MacOS/Safari
524288   305 /System/Applications/Dock.app/Contents/MacOS/Dock
2097152  411 /Applications/Chat.app/Contents/MacOS/Chat
"""


def _ps_answers(fake_run):
    """heavy_consumers calls ps twice: first with pcpu, then with rss."""
    fake_run.answers["ps"] = [(True, PS_CPU), (True, PS_RSS)]


def test_top_consumers_by_cpu_are_sorted_and_named(fake_run):
    _ps_answers(fake_run)
    result = maintenance.heavy_consumers()
    top = result["top_by_cpu"]
    assert [r["command"] for r in top][:2] == ["Safari", "Chat"]
    assert top[0]["pid"] == 201
    assert top[0]["cpu_percent"] == 12.5
    # Full paths are noise in a table; only the executable name belongs.
    assert "/" not in top[0]["command"]


def test_a_garbage_cpu_row_is_skipped_not_fatal(fake_run):
    """The 'not-a-number' row in the fixture must not break the parse."""
    _ps_answers(fake_run)
    result = maintenance.heavy_consumers()
    assert all(isinstance(r["cpu_percent"], float) for r in result["top_by_cpu"])


def test_ram_ranking_is_independent_of_cpu_ranking(fake_run, monkeypatch):
    responses = iter([(True, PS_CPU), (True, PS_RSS)])
    monkeypatch.setattr(
        maintenance, "_run", lambda cmd, timeout=10.0: next(responses)
    )
    result = maintenance.heavy_consumers()
    by_ram = result["top_by_ram"]
    assert by_ram[0]["command"] == "Chat"
    assert by_ram[0]["rss_human"] == "2.0 GB"
    assert "rss_bytes" not in by_ram[0], "the raw value should be humanised"


def test_the_limit_is_respected(fake_run):
    _ps_answers(fake_run)
    assert len(maintenance.heavy_consumers(limit=1)["top_by_cpu"]) == 1


def test_a_failing_ps_gives_empty_lists_not_a_crash(fake_run):
    fake_run.answers["ps"] = (False, "ps: command not found")
    result = maintenance.heavy_consumers()
    assert result["top_by_cpu"] == []
    assert result["top_by_ram"] == []


# ── memory_report ─────────────────────────────────────────────────────────────

VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384)
Pages free:                              250000.
Pages active:                            400000.
Pages inactive:                          100000.
Pages speculative:                        50000.
Pages wired down:                        200000.
Pages occupied by compressor:            150000.
Pages purgeable:                          30000.
"""

MEMORY_PRESSURE = "System-wide memory free percentage: 23%\n"


def test_memory_report_reads_vm_stat_with_the_real_page_size(fake_run):
    fake_run.answers["vm_stat"] = (True, VM_STAT)
    fake_run.answers["memory_pressure"] = (True, MEMORY_PRESSURE)
    result = maintenance.memory_report()
    assert result["free_percent"] == 23
    # 250000 free pages × 16384 bytes, not × 4096.
    assert result["pages_bytes"]["free"] == _mb(250000 * 16384)
    assert result["purgeable_memory"] == _mb(30000 * 16384)


def _mb(nbytes: int) -> str:
    from mac_cleaner.maintenance import _human

    return _human(nbytes)


def test_a_missing_page_size_falls_back_to_4k(fake_run):
    fake_run.answers["vm_stat"] = (True, "Pages free: 100.\n")
    fake_run.answers["memory_pressure"] = (True, MEMORY_PRESSURE)
    assert maintenance.memory_report()["pages_bytes"]["free"] == _mb(100 * 4096)


def test_missing_free_percentage_is_none_not_zero(fake_run):
    """0% free and 'unknown' mean very different things to a user."""
    fake_run.answers["vm_stat"] = (True, VM_STAT)
    fake_run.answers["memory_pressure"] = (False, "")
    assert maintenance.memory_report()["free_percent"] is None


def test_vm_stat_failing_entirely(fake_run):
    fake_run.answers["vm_stat"] = (False, "")
    fake_run.answers["memory_pressure"] = (False, "")
    result = maintenance.memory_report()
    assert result["pages_bytes"] == {}
    assert result["purgeable_memory"] == _mb(0)


# ── tm_snapshots ──────────────────────────────────────────────────────────────

TMUTIL_LIST = """\
Snapshots for volume group containing disk1s1:
com.apple.TimeMachine.2026-09-28-030000.local
com.apple.TimeMachine.2026-09-20-030000.local
"""


def test_local_snapshots_are_listed(fake_run):
    fake_run.answers["tmutil"] = (True, TMUTIL_LIST)
    result = maintenance.tm_snapshots()
    assert result["count"] == 2
    assert result["snapshots"] == ["2026-09-28-030000", "2026-09-20-030000"]
    assert result["note"], "an empty disk must still explain itself"


def test_no_snapshots_says_so_rather_than_looking_broken(fake_run):
    fake_run.answers["tmutil"] = (True, "Snapshots for disk1s1:\n")
    result = maintenance.tm_snapshots()
    assert result["count"] == 0
    assert "nothing" in result["note"].lower()


def test_tmutil_missing_reports_no_snapshots_rather_than_guessing(fake_run):
    fake_run.answers["tmutil"] = (False, "command not found")
    result = maintenance.tm_snapshots()
    assert result["count"] == 0
    assert result["snapshots"] == []


# ── purgeable_space ───────────────────────────────────────────────────────────

def test_purgeable_space_reads_diskutil(fake_run):
    fake_run.answers["diskutil"] = (True, "   Purgeable Space: 2.1 GB\n")
    result = maintenance.purgeable_space()
    assert result["purgeable_space"] == "2.1 GB"
    assert result["disk_free"]
    assert "purgeable" in result["note"].lower()


def test_diskutil_without_the_line_says_unknown_not_zero(fake_run):
    fake_run.answers["diskutil"] = (True, "Device Node: /dev/disk1\n")
    assert maintenance.purgeable_space()["purgeable_space"] == "unknown"

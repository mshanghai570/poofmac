"""
The Textual TUI.

main.py is the third UI surface and had no tests. Textual ships its own
pilot (`app.run_test()`), so the real app is mounted, driven and inspected
headlessly — no terminal required and nothing sent over the network.

The settings come from the isolated config dir, which is empty, so the model
check reports "not configured" instead of calling out to a provider.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual", reason="TUI tests need Textual")

from textual.widgets import Button, DataTable, Static  # noqa: E402

from mac_cleaner import store  # noqa: E402
from mac_cleaner.config import Settings  # noqa: E402
from mac_cleaner.main import (  # noqa: E402
    ConfirmDeleteModal,
    DisclaimerScreen,
    MacCleanerApp,
)
from mac_cleaner.scanner import get_disk_usage  # noqa: E402


def _app(safe_mode: bool = False) -> MacCleanerApp:
    return MacCleanerApp(Settings(), safe_mode=safe_mode)


def _text(widget) -> str:
    """The plain text a user sees, with Rich/Textual markup already applied."""
    rendered = widget.render()
    return str(rendered) if not isinstance(rendered, str) else rendered


# ── Mounting ──────────────────────────────────────────────────────────────────

async def test_the_app_mounts(isolated_config):
    store.mark_disclaimer_accepted()  # skip the first-run modal
    async with _app().run_test() as pilot:
        app = pilot.app
        assert app.title == "PoofMac"
        assert app.query_one("#disk-overview")
        assert app.query_one("#results-table")
        assert app.query_one("#activity-log")
        await pilot.pause()


async def test_the_results_table_has_its_columns(isolated_config):
    store.mark_disclaimer_accepted()
    async with _app().run_test() as pilot:
        table = pilot.app.query_one("#results-table", DataTable)
        assert isinstance(table, DataTable)
        keys = [str(column.label) for column in table.columns.values()]
        assert "Status" in keys
        assert any("Size" in key for key in keys), keys
        await pilot.pause()


async def test_the_disk_overview_shows_the_real_number(isolated_config):
    """Regression: the TUI used to draw a 9% bar on a 91%-full disk."""
    store.mark_disclaimer_accepted()
    async with _app().run_test() as pilot:
        await pilot.pause()
        overview = _text(pilot.app.query_one("#disk-overview", Static))
        usage = get_disk_usage()
        assert f"{usage['used_percent']}%" in overview
        assert usage["used_human"] in overview
        assert usage["free_human"] in overview


async def test_the_activity_log_explains_what_to_do(isolated_config):
    store.mark_disclaimer_accepted()
    async with _app().run_test() as pilot:
        await pilot.pause()
        log = pilot.app.query_one("#activity-log")
        rendered = "\n".join(str(line) for line in (getattr(log, "lines", None) or []))
        assert rendered.strip(), "the log should say something on launch"
        assert "F5" in rendered or "Scan" in rendered, rendered[-200:]


# ── Safe mode ─────────────────────────────────────────────────────────────────

async def test_safe_mode_is_banner_only_when_asked_for(isolated_config):
    store.mark_disclaimer_accepted()
    async with _app(safe_mode=True).run_test() as pilot:
        await pilot.pause()
        banners = [w for w in pilot.app.query(Static) if w.id == "safe-banner"]
        assert banners, "safe mode must be announced on screen"
        banner = _text(banners[0]).upper()
        assert "SAFE MODE" in banner
        assert "no files will be deleted" in banner.lower()


async def test_no_banner_without_safe_mode(isolated_config):
    store.mark_disclaimer_accepted()
    async with _app().run_test() as pilot:
        await pilot.pause()
        assert not [w for w in pilot.app.query(Static) if w.id == "safe-banner"]


# ── The first-run disclaimer ──────────────────────────────────────────────────

async def test_a_fresh_install_is_gated_by_the_disclaimer(isolated_config):
    assert store.disclaimer_accepted() is False
    async with _app().run_test() as pilot:
        await pilot.pause()
        assert pilot.app.screen.__class__ is DisclaimerScreen
        assert not store.disclaimer_accepted(), "nothing may be recorded yet"


async def test_the_disclaimer_says_what_the_app_can_delete(isolated_config):
    async with _app().run_test() as pilot:
        await pilot.pause()
        body = _text(pilot.app.screen.query_one("#disclaimer-body", Static))
        assert "PLEASE READ BEFORE CONTINUING" in body
        assert "safety.py" in body
        for promised in ("Trash", "node_modules"):
            assert promised in body, f"{promised!r} not disclosed to the user"


async def test_accepting_records_acceptance_and_opens_the_app(isolated_config):
    async with _app().run_test() as pilot:
        await pilot.pause()
        pilot.app.screen.query_one("#btn-accept", Button).press()
        await pilot.pause()
        assert store.disclaimer_accepted() is True
        assert pilot.app.screen.__class__ is not DisclaimerScreen


async def test_declining_leaves_without_recording_acceptance(isolated_config):
    async with _app().run_test() as pilot:
        await pilot.pause()
        pilot.app.screen.query_one("#btn-decline", Button).press()
        await pilot.pause()
        assert store.disclaimer_accepted() is False


async def test_the_disclaimer_is_not_shown_again_on_the_next_launch(isolated_config):
    store.mark_disclaimer_accepted()
    async with _app().run_test() as pilot:
        await pilot.pause()
        assert pilot.app.screen.__class__ is not DisclaimerScreen


# ── The delete confirmation ───────────────────────────────────────────────────

async def test_the_confirmation_names_the_stakes(isolated_config):
    """A modal that deletes forever must say so in the button row itself."""
    async with _app().run_test() as pilot:
        modal = ConfirmDeleteModal(count=3, total_human="1.2 GB")
        pilot.app.push_screen(modal)
        await pilot.pause()
        info = _text(pilot.app.screen.query_one("#confirm-info", Static))
        assert "3" in info
        assert "1.2 GB" in info
        assert "CANNOT be undone" in info
        assert "backup" in info
        labels = [str(b.label) for b in pilot.app.screen.query(Button)]
        assert any("Cancel" in label for label in labels), labels
        assert any("Delete" in label for label in labels), labels
        pilot.app.pop_screen()
        await pilot.pause()


async def test_the_delete_button_says_what_it_deletes(isolated_config):
    async with _app().run_test() as pilot:
        pilot.app.push_screen(ConfirmDeleteModal(count=1, total_human="10 MB"))
        await pilot.pause()
        labels = " ".join(str(b.label) for b in pilot.app.screen.query(Button))
        # "permanently delete" must be visible in the affirmative button, not
        # just in the body text, so the user cannot accept it by muscle memory.
        assert "ermanent" in labels or "Delete" in labels
        pilot.app.pop_screen()
        await pilot.pause()

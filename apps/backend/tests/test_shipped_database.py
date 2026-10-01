"""The startup check in front of the shipped SQLite file.

The unit ships a database and hosting platforms upload *over* the previous
release, so whatever the release before it left beside that file is still there:
because every connection sets ``journal_mode=WAL``, that means a ``-wal`` and a
``-shm``. A freshly uploaded main file next to a foreign write-ahead log is a
corrupt database, and it is one that answers ``PRAGMA integrity_check`` with
``ok`` — so the trust decision cannot be delegated to that pragma, and these
tests pin the behaviour instead.

What makes deleting the file the right response rather than refusing to boot is
that the shipped file carries schema and no rows: ``create_all`` rebuilds the
schema and the first incident is seeded from the scenario definitions.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from opspilot_backend import main


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the app at a database file this test controls."""
    path = tmp_path / "opspilot.db"
    monkeypatch.setattr(
        main, "settings", SimpleNamespace(database_url=f"sqlite+aiosqlite:///{path.as_posix()}")
    )
    return path


def _healthy(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE incidents (id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO incidents (title) VALUES ('故障')")
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite+aiosqlite:///./opspilot.db", Path("opspilot.db")),
        ("sqlite+aiosqlite:///:memory:", None),
        ("postgresql+asyncpg://host/db", None),
    ],
)
def test_only_real_files_behind_sqlite_urls_get_checked(url: str, expected: Path | None) -> None:
    assert main._sqlite_path(url) == expected


def test_a_healthy_database_is_left_exactly_as_it_is(wired: Path) -> None:
    _healthy(wired)
    before = wired.read_bytes()

    state = main._verify_shipped_database()

    assert state.endswith("verified")
    assert wired.read_bytes() == before, "a passing check must not rewrite the file"


def test_a_database_whose_schema_vanished_is_rebuilt(wired: Path) -> None:
    """Measured: a stale WAL presents as exactly this, with ``integrity_check`` ok."""
    sqlite3.connect(wired).close()

    state = main._verify_shipped_database()

    assert "schema is missing" in state
    assert not wired.exists()


def test_a_file_that_is_not_a_database_is_rebuilt(wired: Path) -> None:
    wired.write_bytes(b"this is not a SQLite file")

    state = main._verify_shipped_database()

    assert "unusable" in state
    assert not wired.exists()


def test_the_stale_wal_pairing_is_never_trusted(wired: Path) -> None:
    """The deploy-time failure: new main file, previous release's write-ahead log.

    The assertion is deliberately just "deleted" rather than "matched reason X",
    because the error this produces is not stable — sometimes a vanished schema
    under an ``ok`` pragma, sometimes a raw ``DatabaseError``. Both have to end
    the same way.
    """
    _healthy(wired)
    wired.with_name("opspilot.db-wal").write_bytes(b"write-ahead log from the previous release")
    wired.with_name("opspilot.db-shm").write_bytes(b"shared memory from the previous release")

    state = main._verify_shipped_database()

    if "verified" in state:
        # A clean read is possible in principle; then the file has to be whole
        # and the row has to still be there. A clean read that loses the table
        # is the failure this whole function exists to catch.
        assert wired.exists()
        conn = sqlite3.connect(f"file:{wired.as_posix()}?mode=ro", uri=True)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
        finally:
            conn.close()
    else:
        assert not wired.exists()


def test_a_wal_left_with_no_database_is_cleared(wired: Path) -> None:
    wired.with_name("opspilot.db-wal").write_bytes(b"orphaned write-ahead log")

    state = main._verify_shipped_database()

    assert "absent" in state
    assert not wired.with_name("opspilot.db-wal").exists()


def test_a_first_ever_start_is_not_reported_as_a_problem(wired: Path) -> None:
    """No file yet is the normal case for a dev box, and must not read as damage."""
    state = main._verify_shipped_database()

    assert "absent" in state
    assert not wired.exists()


# --- The build stamp ---------------------------------------------------------
# This is the check that actually distinguishes "our database" from "the previous
# release's database", because a foreign `-wal` makes the two indistinguishable:
# same schema, previous rows, and `integrity_check` answering `ok`.


def test_a_database_from_another_release_is_rebuilt(
    wired: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _healthy(wired)
    monkeypatch.setenv("OPSPILOT_BUILD_STAMP", "1700000000")

    state = main._verify_shipped_database()

    assert "build stamp mismatch" in state
    assert not wired.exists()


def test_a_database_from_this_release_is_verified(
    wired: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _healthy(wired)
    monkeypatch.setenv("OPSPILOT_BUILD_STAMP", "1700000000")
    main._stamp_database_file()

    assert main._verify_shipped_database().endswith("verified")


def test_the_sidecars_go_with_it_when_the_stamp_does_not_match(
    wired: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting only the main file would let the foreign WAL take over again.

    The next `create_all` would build its tables into a brand-new file, and the
    write-ahead log still sitting beside it would override them — the same
    failure, one release later.
    """
    _healthy(wired)
    wired.with_name("opspilot.db-wal").write_bytes(b"foreign write-ahead log")
    wired.with_name("opspilot.db-shm").write_bytes(b"foreign shared memory")
    monkeypatch.setenv("OPSPILOT_BUILD_STAMP", "1700000000")

    main._verify_shipped_database()

    assert not wired.exists()
    assert not wired.with_name("opspilot.db-wal").exists()
    assert not wired.with_name("opspilot.db-shm").exists()


def test_a_rebuilt_database_is_stamped_so_the_next_boot_keeps_it(
    wired: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise a one-time repair becomes permanent data loss on every restart."""
    monkeypatch.setenv("OPSPILOT_BUILD_STAMP", "1700000000")
    _healthy(wired)

    main._stamp_database_file()

    conn = sqlite3.connect(f"file:{wired.as_posix()}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1700000000
    finally:
        conn.close()
    assert main._verify_shipped_database().endswith("verified")


def test_no_stamp_configured_leaves_local_databases_alone(
    wired: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dev and tests must not acquire a rebuild-every-boot behaviour.

    The stamp only exists in a built deploy unit's `.env`; when it is absent
    there is nothing to compare against, and an unstamped database is normal.
    """
    monkeypatch.delenv("OPSPILOT_BUILD_STAMP", raising=False)
    _healthy(wired)

    assert main._verify_shipped_database().endswith("verified")
    assert wired.exists()


def test_a_delete_that_fails_does_not_take_the_service_down(
    wired: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deletion is the repair, not the contract.

    A read-only mount, a permission bit, or another process holding the file all
    make the unlink fail. Any of them is survivable — the app can still create
    its schema and the health endpoint still reports the real state — whereas
    raising here kills the process during startup, which is the one outcome with
    no recovery path at all.
    """
    sqlite3.connect(wired).close()  # no schema: the check will want it gone
    original = Path.unlink

    def refuse(self: Path, missing_ok: bool = False) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "unlink", refuse)
    try:
        state = main._verify_shipped_database()
    finally:
        monkeypatch.setattr(Path, "unlink", original)

    assert "could not be replaced" in state
    assert wired.exists()

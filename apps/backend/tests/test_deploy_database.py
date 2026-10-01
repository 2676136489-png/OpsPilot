"""What the build script does to the database it ships.

Two defects in a row came out of this one function, and neither announced
itself:

* ``shutil.copy2`` copies the ``.db`` file and nothing else. SQLite keeps
  committed-but-uncheckpointed transactions in a ``-wal`` sidecar, so the copy
  arrived missing pages the main file still referenced. That is not a stale
  copy, it is a *malformed* one, and SQLite says so at first write rather than
  at open — so the symptom was "the hosted app 500s after a while", three
  steps away from the cause.
* The same copy shipped ``apps/backend/opspilot.db``, the development
  database, rows and all. Those rows were English and dated, and their
  presence disabled ``_seed_demo_incidents()`` — which is guarded on
  ``count(incidents) == 0`` and is the only thing that gives a fresh unit a
  first screen at all.

So the two properties worth asserting are: the copy is self-consistent no
matter what the source's journal state is, and it carries schema without
carrying rows.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "_build_deploy_under_test", ROOT / "scripts" / "build_deploy.py"
)
assert _SPEC and _SPEC.loader
build_deploy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build_deploy)


def _tables(path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return [
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
    finally:
        conn.close()


def _row_count(path: Path, table: str) -> int:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def source_with_wal(tmp_path: Path):
    """A database whose newest rows live in a ``-wal``, as a live one's do.

    The connection stays open on purpose: closing the last connection
    checkpoints the WAL into the main file and deletes the sidecar, which is
    exactly the state that hides the bug.
    """
    path = tmp_path / "source.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE incidents (id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, note TEXT)")
    with conn:
        conn.executemany(
            "INSERT INTO incidents (title) VALUES (?)",
            [(f"故障 {i}",) for i in range(400)],
        )
        conn.executemany(
            "INSERT INTO evidence (note) VALUES (?)", [(f"note-{i}",) for i in range(50)]
        )
    try:
        yield path
    finally:
        conn.close()


def test_the_fixture_actually_produces_a_wal(source_with_wal: Path) -> None:
    # Without this the rest of the file could pass by testing a plain copy.
    assert source_with_wal.with_name("source.db-wal").is_file()


def test_snapshot_reads_through_the_wal(source_with_wal: Path, tmp_path: Path) -> None:
    dest = tmp_path / "shipped.db"
    build_deploy.snapshot_sqlite(source_with_wal, dest)

    assert build_deploy.check_sqlite(dest) == "ok"
    assert _row_count(dest, "incidents") == 400
    assert _row_count(dest, "evidence") == 50


def test_snapshot_leaves_no_sidecar_behind(source_with_wal: Path, tmp_path: Path) -> None:
    """A surviving ``-wal`` is how a *previous* build corrupts the *next* one."""
    dest = tmp_path / "shipped.db"
    build_deploy.snapshot_sqlite(source_with_wal, dest)
    assert not dest.with_name("shipped.db-wal").exists()
    assert not dest.with_name("shipped.db-shm").exists()


def test_snapshot_overwrites_a_stale_sidecar_from_a_previous_build(
    source_with_wal: Path, tmp_path: Path
) -> None:
    """The hazard that produced ``btreeInitPage() returns error code 1``.

    A stale WAL next to a freshly copied main file describes a database that
    looks sound and is not, so the write path has to clear it rather than
    inherit it.

    ``integrity_check`` is asserted here but deliberately not leaned on: when
    this scenario was measured against the old ``shutil.copy2`` path, the pragma
    answered ``ok`` and the query afterwards raised ``no such table:
    incidents``. Passing the pragma is not evidence of a usable database, which
    is why the row count is the assertion that actually discriminates.
    """
    dest = tmp_path / "shipped.db"
    dest.write_bytes(b"not a real database")
    dest.with_name("shipped.db-wal").write_bytes(b"stale write-ahead log")
    dest.with_name("shipped.db-shm").write_bytes(b"stale shared memory")

    build_deploy.snapshot_sqlite(source_with_wal, dest)

    assert build_deploy.check_sqlite(dest) == "ok"
    assert _row_count(dest, "incidents") == 400
    assert _row_count(dest, "evidence") == 50


def test_check_sqlite_rejects_a_database_with_no_schema(tmp_path: Path) -> None:
    """The build gate has to fail loudly, not pass on a technicality.

    An empty SQLite file is valid, openable, and passes ``integrity_check`` —
    and shipping one would hand the sandbox a unit whose tables are built from
    scratch at import time. ``check_sqlite`` is the only thing standing between
    that and a published deploy, so it must not answer ``ok``.
    """
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()

    state = build_deploy.check_sqlite(empty)

    assert state != "ok"
    assert "schema" in state


def test_reset_empties_rows_and_keeps_schema(source_with_wal: Path, tmp_path: Path) -> None:
    dest = tmp_path / "shipped.db"
    build_deploy.snapshot_sqlite(source_with_wal, dest)
    before = _tables(dest)

    emptied = build_deploy.reset_sqlite(dest)

    assert emptied == len(before)
    assert _tables(dest) == before, "schema must survive; only rows go"
    for table in before:
        assert _row_count(dest, table) == 0
    assert build_deploy.check_sqlite(dest) == "ok"


def test_reset_survives_a_schema_with_no_autoincrement(tmp_path: Path) -> None:
    """``sqlite_sequence`` only exists once some table used AUTOINCREMENT."""
    path = tmp_path / "plain.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t VALUES ('a', 'b')")
    conn.commit()
    conn.close()

    assert build_deploy.reset_sqlite(path) == 1
    assert _row_count(path, "t") == 0


def test_reset_is_vacuumed_so_the_unit_is_not_padded(tmp_path: Path) -> None:
    """Deleted rows leave pages on the free list; the shipped file is uploaded."""
    path = tmp_path / "fat.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, blob TEXT)")
    with conn:
        conn.executemany("INSERT INTO t (blob) VALUES (?)", [("x" * 2000,) for _ in range(2000)])
    conn.commit()
    padded = path.stat().st_size
    conn.close()

    build_deploy.reset_sqlite(path)

    assert path.stat().st_size < padded / 2


# --- The build stamp ---------------------------------------------------------


def test_stamp_round_trips_through_the_database_header(tmp_path: Path) -> None:
    path = tmp_path / "shipped.db"
    sqlite3.connect(path).close()

    build_deploy.stamp_database(path, 1_700_000_000)

    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1_700_000_000
    finally:
        conn.close()


def test_stamp_survives_vacuum_and_survives_reset_order(tmp_path: Path) -> None:
    """The stamp is written last for a reason: a rebuild must not erase it."""
    path = tmp_path / "shipped.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO t (id) VALUES (1)")
    conn.commit()
    conn.close()

    build_deploy.reset_sqlite(path)
    build_deploy.stamp_database(path, 1_700_000_001)
    build_deploy.reset_sqlite(path)

    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1_700_000_001
    finally:
        conn.close()


def test_the_stamp_line_is_written_and_never_carried_forward() -> None:
    """A stamp reused across builds would let a stale database pass as current."""
    first = build_deploy.build_dotenv({}, 1_700_000_000)
    second = build_deploy.build_dotenv({"OPSPILOT_BUILD_STAMP": "1700000000"}, 1_700_000_999)

    assert "OPSPILOT_BUILD_STAMP=1700000000" in first
    assert "OPSPILOT_BUILD_STAMP=1700000999" in second
    assert second.count("OPSPILOT_BUILD_STAMP=") == 1


def test_forwarded_credentials_still_win_over_previous_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stamp is new; the shell-wins rule for secrets must not have changed."""
    for name in build_deploy.FORWARDED_ENV:
        monkeypatch.delenv(name, raising=False)

    text = build_deploy.build_dotenv({"OPENAI_API_KEY": "old"}, 1_700_000_000)

    assert "OPENAI_API_KEY=old" in text


def test_a_credential_exported_right_now_beats_the_previous_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fresh")

    text = build_deploy.build_dotenv({"OPENAI_API_KEY": "old"}, 1_700_000_000)

    assert "OPENAI_API_KEY=fresh" in text
    assert "old" not in text

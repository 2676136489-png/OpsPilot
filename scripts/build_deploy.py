"""Assemble `deploy/` — the single-port unit that gets published.

The repo itself cannot be uploaded as-is:

* `apps/backend/.venv` is ~97 MB of local virtualenv.
* `apps/frontend/dist/` is build output and is git-ignored (publishing tools
  skip build output too).
* The repo root carries a `docker-compose.yml` declaring Postgres and Redis,
  which the hosting sandbox does not provide.

So this script copies only what the running service needs and renames `dist/`
to `webroot/` along the way, which is both the path the backend looks for and
a name no build-output filter recognises.

Usage:
    python scripts/build_deploy.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "apps" / "backend"
FRONTEND = ROOT / "apps" / "frontend"
DEPLOY = ROOT / "deploy"

# `.env` is read by pydantic-settings from the process working directory, which
# is the deploy root — this is how demo seeding is enabled for the hosted
# instance only, without touching local behaviour or tests.
BASE_ENV = """APP_ENV=production
APP_DEBUG=false
OPSPILOT_DEMO_SEED=true
# Hosting targets expose ONE port. Without this the provider client dials the
# default http://127.0.0.1:8100 for a companion simulator process that does not
# exist in the sandbox, and every /api/v1/services call 500s. With it the
# simulator is mounted in-process at /__sim and repointed at this port (see the
# bootstrap block at the top of opspilot_backend.main).
OPSPILOT_EMBED_SIMULATOR=true
"""
# Forwarded from the operator's own shell when present, never written here.
# Keeping them out of the source tree is the point: no key ever enters git.
# `deploy/` is git-ignored, so the value only lands in the upload unit.
FORWARDED_ENV = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "OPSPILOT_LLM_PROVIDER",
    "OPENAI_TEMPERATURE",
)

# Names the release that built the unit, so the service can tell whether the
# database it just opened is the one this release shipped.
#
# It has to: hosting platforms upload *over* the previous release, and SQLite
# keeps committed pages in a `-wal` sidecar. The release that ran before this
# one left one behind (every connection sets `journal_mode=WAL`), the new main
# file replaces the old one, and the old WAL stays. A write-ahead log carries a
# copy of page 1, so SQLite happily serves the *previous* database — measured:
# a schema-only file paired with a foreign WAL reported the foreign schema's
# table count and its 300 rows, with `PRAGMA integrity_check` answering `ok`.
# Nothing about that database says "previous release"; the stamp does.
BUILD_STAMP_ENV = "OPSPILOT_BUILD_STAMP"


def read_previous_env() -> dict[str, str]:
    """Value of each forwarded name in the `deploy/.env` about to be deleted.

    Regenerating `.env` from the shell alone is not enough. The key normally
    only ever exists in `deploy/.env` — it is git-ignored on purpose and the
    shell that exported it during the first build is usually gone by the time
    someone rebuilds. Rebuilding then replaced a working key with nothing, and
    the only symptom is an agent that quietly answers from templates with
    `usage.tokens == 0`. Carrying the previous values forward makes a rebuild
    non-destructive to credentials.
    """
    path = DEPLOY / ".env"
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if name in FORWARDED_ENV and value:
            found[name] = value
    return found


def build_dotenv(previous: dict[str, str], stamp: int) -> str:
    lines = [BASE_ENV.rstrip("\n")]
    # The stamp is regenerated on every build — that is the point of it, so it
    # is never carried forward from the previous `.env`.
    lines.append(f"{BUILD_STAMP_ENV}={stamp}")
    for name in FORWARDED_ENV:
        # A value exported right now wins; otherwise the last build's value is
        # kept. Neither source can ever write into the source tree.
        value = (os.environ.get(name) or previous.get(name, "")).strip()
        if value:
            lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def snapshot_sqlite(src: Path, dest: Path) -> None:
    """Write `dest` as one self-consistent copy of the database at `src`.

    ``shutil.copy2`` copies the ``.db`` file and nothing else. SQLite keeps
    committed-but-not-yet-checkpointed transactions in a ``-wal`` sidecar, so a
    plain file copy can arrive at the target missing pages the main file still
    references — and SQLite then reports it as ``malformed``, not as stale.

    Worse, the target may still hold a ``-wal`` from the *previous* build. A
    stale WAL combined with a freshly copied main file is a database that looks
    structurally sound and is not. That is exactly how the hosted unit's seeded
    database ended up with ``btreeInitPage() returns error code 1``.

    ``Connection.backup()`` reads through the WAL and emits a single
    self-consistent file, which is why it is used instead of a byte copy.
    """
    for path in (dest, dest.with_name(dest.name + "-wal"), dest.with_name(dest.name + "-shm")):
        path.unlink(missing_ok=True)

    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
            # The backup inherits the source's header, WAL mode included. Put the
            # copy back on the default rollback journal so the shipped unit is a
            # single file with no `-wal`/`-shm` sitting next to it — a sidecar
            # that survives into the next build is precisely the hazard this
            # function exists to remove.
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
    finally:
        source.close()

    for sidecar in (dest.with_name(dest.name + "-wal"), dest.with_name(dest.name + "-shm")):
        sidecar.unlink(missing_ok=True)


def reset_sqlite(path: Path) -> int:
    """Delete every row in `path`, leaving the schema behind.

    The build used to ship `apps/backend/opspilot.db` verbatim — the
    *development* database. That file carries four incidents left over from
    local runs, complete with the titles and descriptions of the time, and it is
    not regenerable in the sense that matters: those rows are history, not
    source. Shipping them also silently disarmed `_seed_demo_incidents()` in
    `opspilot_backend.main`, which is documented to seed "one canonical incident
    so a fresh deploy isn't an empty dashboard" but guards on
    ``count(incidents) == 0`` — a guard that never passed.

    Emptying every table hands the first screen back to that seed, which reads
    its text from `opspilot_simulator.scenarios`. It also keeps the shipped unit
    free of whatever a local run happened to leave behind, including service
    names and approval records nobody meant to publish.

    Returns the number of tables emptied.
    """
    conn = sqlite3.connect(path)
    try:
        # Off, because the wipe order is arbitrary and we want no ordering
        # constraint at all — every row goes.
        conn.execute("PRAGMA foreign_keys=OFF")
        tables = [
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for name in tables:
            conn.execute(f'DELETE FROM "{name}"')
        try:
            # Only exists once some table has used AUTOINCREMENT; a fresh
            # schema without one has no such table, and that is not an error.
            conn.execute("DELETE FROM sqlite_sequence")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        # Reclaims the pages the deleted rows held, so the published unit is
        # small rather than a full-size file with a mostly-empty free list.
        conn.execute("VACUUM")
    finally:
        conn.close()
    return len(tables)


def stamp_database(path: Path, stamp: int) -> None:
    """Write `stamp` into the database header's ``user_version`` field.

    SQLite stores this 32-bit word and never interprets it, which makes it the
    right place for "which release wrote this file": no schema change, no row to
    keep in sync, and it travels with the file. It also travels into a ``-wal``,
    and that is where the comparison earns its keep — when a foreign
    write-ahead log overrides the uploaded main file, the header SQLite serves
    is the *foreign* one, stamp included.
    """
    conn = sqlite3.connect(path)
    try:
        # SQLite reads this as a signed 32-bit value; keep it in range rather
        # than letting a future timestamp wrap into a negative.
        conn.execute(f"PRAGMA user_version = {int(stamp) & 0x7FFFFFFF}")
        conn.commit()
    finally:
        conn.close()


def check_sqlite(path: Path) -> str:
    """``PRAGMA integrity_check`` on the copy that is about to ship.

    A malformed database is not a loud failure. The service starts, the SPA
    loads, ``/api/v1/health`` answers, and the first request that touches the
    bad page returns a 500 — so the fault surfaces as "the app is flaky" long
    after the build that caused it. Testing it here costs one query and keeps
    the cause next to the effect.

    The pragma alone is not sufficient, and that is measured rather than
    assumed: with a stale ``-wal`` sitting beside a freshly copied main file,
    ``integrity_check`` answered ``ok`` for a database whose ``incidents`` table
    had vanished. Opening it succeeded, the pragma passed, the schema was gone.
    So the schema is asserted separately — a unit that ships without one is not
    a unit that passed its checks.
    """
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            return integrity
        tables = int(
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    return "ok" if tables else "ok but the schema is missing (0 tables)"


def copy_tree(src: Path, dest: Path) -> int:
    """Copy `src` into `dest`, skipping caches. Returns the file count."""
    count = 0
    for item in src.rglob("*"):
        if any(p in {"__pycache__", ".pytest_cache", ".ruff_cache"} for p in item.parts):
            continue
        if item.is_dir():
            continue
        target = dest / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        count += 1
    return count


def main() -> int:
    dist = FRONTEND / "dist"
    if not (dist / "index.html").is_file():
        print("error: frontend not built — run `npm run build` first", file=sys.stderr)
        return 1

    previous_env = read_previous_env()
    DEPLOY.mkdir(parents=True, exist_ok=True)

    # Seconds since the epoch, and deliberately not something stable across
    # rebuilds: two builds must never share a stamp, or a stale database would
    # look like a current one.
    stamp = int(time.time())

    # `.env` goes down first, before anything is deleted. It is the one file in
    # the unit that cannot be regenerated from the repository — the key lives
    # here and nowhere else — so a rebuild that dies part-way must not be able to
    # take it with it. That is not hypothetical: `shutil.rmtree(DEPLOY)` was
    # observed aborting mid-walk because a server started from `deploy/` still
    # held the SQLite file, which left a half-deleted unit with no `.env` and no
    # signal that anything had gone wrong.
    (DEPLOY / ".env").write_text(build_dotenv(previous_env, stamp), encoding="utf-8")

    # The generated parts are replaced one subdirectory at a time instead of by
    # swapping the whole tree, so a locked file fails the subtree it belongs to
    # rather than the unit.
    try:
        for name in ("src", "webroot", "runbooks"):
            stale = DEPLOY / name
            if stale.exists():
                shutil.rmtree(stale)

        n_src = copy_tree(BACKEND / "src", DEPLOY / "src")
        n_web = copy_tree(dist, DEPLOY / "webroot")

        # Runbooks are read from disk at request time, so they have to ship with
        # the unit — otherwise the Runbooks page and the agent's runbook_search
        # tool both find nothing. Because the deploy tree is one level shallower
        # than the source tree, they also have to sit where the lookup walk can
        # find them.
        n_run = 0
        runbooks = ROOT / "runbooks"
        if runbooks.is_dir():
            n_run = copy_tree(runbooks, DEPLOY / "runbooks")

        # pyproject.toml is deliberately left out: it declares the `uv_build`
        # backend, and if the host picks it up it will try to *build* the package
        # instead of just installing the pinned requirements.
        for name in ("serve.py", "requirements.txt"):
            if (BACKEND / name).is_file():
                shutil.copy2(BACKEND / name, DEPLOY / name)

        db = BACKEND / "opspilot.db"
        db_state = "none"
        emptied = 0
        if db.is_file():
            snapshot_sqlite(db, DEPLOY / "opspilot.db")
            emptied = reset_sqlite(DEPLOY / "opspilot.db")
            stamp_database(DEPLOY / "opspilot.db", stamp)
            db_state = check_sqlite(DEPLOY / "opspilot.db")
            if db_state != "ok":
                print(
                    f"error: the copied database did not pass its checks: {db_state}",
                    file=sys.stderr,
                )
                print(
                    f"       source is {db}. The copy is a faithful read-through of it, "
                    "so the source is in the same state — delete it and let the "
                    "service rebuild the schema, then re-run.",
                    file=sys.stderr,
                )
                return 1
    except OSError as exc:
        print(f"error: deploy/ is only partially rebuilt — {exc}", file=sys.stderr)
        print(
            "hint: something is holding files under deploy/ — usually a server "
            "started from it. Stop that process and re-run.",
            file=sys.stderr,
        )
        print(
            "      deploy/.env was written before the rebuild started and is intact.",
            file=sys.stderr,
        )
        return 1

    from_shell = [n for n in FORWARDED_ENV if os.environ.get(n, "").strip()]
    carried = [
        n
        for n in FORWARDED_ENV
        if n not in from_shell and previous_env.get(n)
    ]
    print(f"deploy/ rebuilt — src {n_src} files, webroot {n_web} files, runbooks {n_run} files")
    print(
        f"database: schema only ({emptied} tables, no rows, checks {db_state}) — "
        "OPSPILOT_DEMO_SEED creates the first incident at startup"
    )
    print(f"build stamp: {stamp} (a database from another release is rebuilt on boot)")
    if from_shell:
        print(f"LLM config from this shell: {', '.join(from_shell)}")
    if carried:
        print(f"LLM config carried over from the previous deploy/.env: {', '.join(carried)}")
    if not from_shell and not carried:
        print("no LLM key anywhere — agent will run deterministic (export OPENAI_API_KEY first to enable it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

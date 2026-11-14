"""SQLite engine and schema setup.

- WAL (write-ahead logging) lets the app window and the Claude Desktop helper read
  and write the same file at once without "database is locked" errors.
- Schema changes are tracked with SQLite's built-in `PRAGMA user_version`. To change
  a table later, append a function to MIGRATIONS; it runs once on existing files.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, event, text
from sqlmodel import Session, SQLModel, create_engine

from . import tables  # noqa: F401  (registers the tables on SQLModel.metadata)
from .paths import data_dir

DB_FILENAME = "schedule.db"

# Each migration takes a connection and upgrades the schema by one version.
# Version 1 = the tables as first created by create_all() in Phase 4.


def _v1_to_v2(conn) -> None:
    """Phase 6: re-plan stability weight and the Optimizer calendar's ID."""
    conn.execute(text("ALTER TABLE preferences ADD COLUMN stability_per_hour FLOAT NOT NULL DEFAULT 1.0"))
    conn.execute(text("ALTER TABLE preferences ADD COLUMN optimizer_calendar_id VARCHAR"))


def _v2_to_v3(conn) -> None:
    """Phase 7: Claude proposals, app-only holds, the last solve's summary, API spending."""
    conn.execute(text("ALTER TABLE preferences ADD COLUMN claude_monthly_cap_usd FLOAT NOT NULL DEFAULT 5.0"))
    for table in (tables.Proposal, tables.Hold, tables.LastSolve, tables.ApiUsage):
        table.__table__.create(conn)


def _v3_to_v4(conn) -> None:
    """Remember whether the last solve was approved, so an empty re-plan can still be approved."""
    # A v2 file upgraded in one go already got the column from v3's create(), which uses today's table.
    columns = {r[1] for r in conn.execute(text("PRAGMA table_info(lastsolve)"))}
    if "approved_at" not in columns:
        conn.execute(text("ALTER TABLE lastsolve ADD COLUMN approved_at DATETIME"))


def _v4_to_v5(conn) -> None:
    """Chat codes: Claude Desktop works only in chats you activate."""
    tables.ChatCode.__table__.create(conn)


def _v5_to_v6(conn) -> None:
    """Done tasks get a completion time (they're deleted 7 days later); Claude Desktop connection time."""
    conn.execute(text("ALTER TABLE task ADD COLUMN completed_at DATETIME"))
    conn.execute(text("ALTER TABLE preferences ADD COLUMN desktop_helper_seen_at DATETIME"))
    # Tasks already done start their 7 days now.
    conn.execute(text("UPDATE task SET completed_at = :now WHERE status = 'DONE'"),
                 {"now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")})  # SQLAlchemy's format


MIGRATIONS: list[Callable] = [_v1_to_v2, _v2_to_v3, _v3_to_v4, _v4_to_v5, _v5_to_v6]
SCHEMA_VERSION = 1 + len(MIGRATIONS)


def db_path() -> Path:
    return data_dir() / DB_FILENAME


def _on_connect(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")  # wait up to 5 s if the other process is writing
    cur.close()


def make_engine(path: Path | None = None) -> Engine:
    path = path or db_path()
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _on_connect)
    _migrate(engine)
    return engine


def _migrate(engine: Engine) -> None:
    with engine.begin() as conn:
        version = conn.execute(text("PRAGMA user_version")).scalar_one()
        if version == 0:
            SQLModel.metadata.create_all(conn)
            conn.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))
            return
        for i in range(version - 1, len(MIGRATIONS)):
            MIGRATIONS[i](conn)
            conn.execute(text(f"PRAGMA user_version = {i + 2}"))


_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


@contextmanager
def session(eng: Engine | None = None) -> Iterator[Session]:
    with Session(eng or engine(), expire_on_commit=False) as s:
        yield s

"""SQLite engine and schema setup.

- WAL (write-ahead logging) lets the app window and the Claude Desktop helper read
  and write the same file at once without "database is locked" errors.
- Schema changes are tracked with SQLite's built-in `PRAGMA user_version`. To change
  a table later, append a function to MIGRATIONS; it runs once on existing files.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, event, text
from sqlmodel import Session, SQLModel, create_engine

from . import tables  # noqa: F401  (registers the tables on SQLModel.metadata)
from .paths import data_dir

DB_FILENAME = "schedule.db"

# Each migration takes a connection and upgrades the schema by one version.
# Version 1 = the tables as first created by create_all().
MIGRATIONS: list[Callable] = []
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

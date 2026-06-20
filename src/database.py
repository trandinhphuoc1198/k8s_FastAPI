import os
import random
import string
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.logging import get_logger
from src.models import Base, TestItem

load_dotenv()

logger = get_logger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "TestDb")

DB_WRITER_HOST = os.getenv("DB_WRITER_HOST", "localhost")
DB_READER_HOST = os.getenv("DB_READER_HOST", "localhost")

DB_DRIVER = os.getenv("DB_DRIVER", "psycopg2")

# ── Helpers ────────────────────────────────────────────────────────────────────

def _build_connection_url(host: str, db_name: str = DB_NAME) -> str:
    """Build a SQLAlchemy connection string for the given host and database."""
    return f"postgresql+{DB_DRIVER}://{DB_USER}:{DB_PASSWORD}@{host}:{DB_PORT}/{db_name}"


def _random_string(length: int = 12) -> str:
    """Generate a random alphanumeric string."""
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


# ── Lazy engines ───────────────────────────────────────────────────────────────
#
# WHY LAZY: engines used to be created at module import time
# (`writer_engine = create_engine(...)`). That forces SQLAlchemy to resolve
# and import the psycopg2 driver the instant `src.database` is imported —
# even just for unit tests that mock everything and never touch a real DB.
# If psycopg2 isn't installed in that environment (e.g. a slim test venv),
# the import itself fails with NoSuchModuleError before a single test runs.
#
# Creating the engine lazily, on first use, means importing this module is
# always safe. Tests can patch `_get_writer_engine` / `_get_reader_engine`
# (or the lower-level `create_engine`) without ever needing the real driver
# to be importable.

_writer_engine: Engine | None = None
_reader_engine: Engine | None = None


def _get_writer_engine() -> Engine:
    global _writer_engine
    if _writer_engine is None:
        _writer_engine = create_engine(_build_connection_url(DB_WRITER_HOST))
    return _writer_engine


def _get_reader_engine() -> Engine:
    global _reader_engine
    if _reader_engine is None:
        _reader_engine = create_engine(_build_connection_url(DB_READER_HOST))
    return _reader_engine


# ── Read/Write routing ─────────────────────────────────────────────────────────
#
# NOTE ON APPROACH — why this does NOT sniff the SQL clause:
#
# An earlier version of this routing tried to inspect the statement passed to
# Session.get_bind(clause=...) and decide reader vs. writer based on whether
# it was a Select vs. an Insert/Update/Delete. That approach is unreliable:
#
#   - ORM unit-of-work flushes (the actual INSERT emitted by
#     `session.add_all(items); session.commit()`) call get_bind() with
#     mapper=<Mapper>, and `clause` is frequently None or not what you'd
#     expect — it depends on SQLAlchemy version and code path.
#   - This caused a real bug: bulk INSERTs were being routed to the READER,
#     which raised "cannot execute INSERT in a read-only transaction".
#
# The reliable, version-proof solution used here is for the CALLER to declare
# intent explicitly via session_scope(mode="read"|"write"). Each call site in
# this file already knows whether it's reading or writing, so there's no need
# to guess from SQL structure at all.


class RoutingSession(Session):
    """
    A Session whose bind is fixed at construction time to either the reader
    or the writer engine, based on an explicit `mode` argument.

    This is intentionally NOT auto-detecting from the SQL clause — see the
    module-level note above for why that approach is unsafe.
    """

    def __init__(self, *args, mode: str = "write", bind_write: Engine = None, bind_read: Engine = None, **kwargs):
        if mode not in ("read", "write"):
            raise ValueError(f"mode must be 'read' or 'write', got {mode!r}")
        self._routing_mode = mode
        self._bind_write = bind_write if bind_write is not None else _get_writer_engine()
        self._bind_read = bind_read if bind_read is not None else _get_reader_engine()
        # expire_on_commit=False so objects remain readable (e.g. for
        # serialization into a dict) immediately after commit, without
        # needing a round-trip refresh from the DB.
        kwargs.setdefault("expire_on_commit", False)
        super().__init__(*args, bind=self._active_bind(), **kwargs)

    def _active_bind(self) -> Engine:
        return self._bind_read if self._routing_mode == "read" else self._bind_write

    def get_bind(self, mapper=None, clause=None, **kwargs):
        bind = self._active_bind()
        logger.debug(
            "routing session bind",
            extra={"db.mode": self._routing_mode, "db.bind": str(bind.url).split("@")[-1]},
        )
        return bind


@contextmanager
def session_scope(mode: str = "write", bind_write: Engine = None, bind_read: Engine = None):
    """
    Open a RoutingSession bound to the writer or reader engine for the
    duration of the `with` block, rolling back (then closing) on error.

    Usage:
        with session_scope(mode="write") as session:
            session.add_all(items)
            session.commit()

        with session_scope(mode="read") as session:
            rows = session.query(TestItem).all()
    """
    session = RoutingSession(mode=mode, bind_write=bind_write, bind_read=bind_read)
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── Main DB functions ──────────────────────────────────────────────────────────

def get_tables() -> list[str]:
    """Fetch all public table names using the reader engine."""
    logger.debug(
        "querying database for tables",
        extra={"db.host": DB_READER_HOST, "db.port": DB_PORT, "db.name": DB_NAME},
    )
    try:
        inspector = inspect(_get_reader_engine())
        tables = sorted(inspector.get_table_names(schema="public"))
        logger.debug("fetched tables", extra={"db.table_count": len(tables)})
        return tables
    except Exception:
        logger.exception("error querying tables", extra={"db.name": DB_NAME})
        raise


# ── Test data functions (use the existing DB_NAME database) ──────────────────

def create_test_table() -> None:
    """
    Create the test_items table in the existing database (DB_NAME) if it
    does not already exist. Uses Base.metadata.create_all() so the schema
    is always derived from the ORM model — no hand-written DDL needed.
    """
    try:
        Base.metadata.create_all(bind=_get_writer_engine())
        logger.info(
            "test table ready",
            extra={"db.name": DB_NAME, "db.table": TestItem.__tablename__},
        )
    except Exception:
        logger.exception(
            "error creating test table",
            extra={"db.name": DB_NAME, "db.table": TestItem.__tablename__},
        )
        raise


def insert_test_data(n: int = 10) -> list[dict]:
    """
    Insert *n* random TestItem rows into the test table.
    Always routed to the writer (mode="write") — never guessed.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")

    items = [
        TestItem(
            id=uuid.uuid4(),
            name=_random_string(),
            value=random.randint(1, 10_000),
            created_at=datetime.now(tz=timezone.utc),
        )
        for _ in range(n)
    ]

    try:
        with session_scope(mode="write") as session:
            session.add_all(items)
            session.commit()
            logger.info(
                "inserted test data",
                extra={
                    "db.name": DB_NAME,
                    "db.table": TestItem.__tablename__,
                    "db.row_count": n,
                },
            )
            # Build the result while objects are still attached to the
            # session — don't rely on attribute access working after the
            # session closes.
            result = [item.to_dict() for item in items]
        return result
    except Exception:
        logger.exception(
            "error inserting test data",
            extra={"db.name": DB_NAME, "db.table": TestItem.__tablename__},
        )
        raise


def get_test_records() -> list[dict]:
    """
    Fetch all TestItem rows ordered by created_at descending.
    Always routed to the reader (mode="read") — never guessed.
    """
    try:
        with session_scope(mode="read") as session:
            items = (
                session.query(TestItem)
                .order_by(TestItem.created_at.desc())
                .all()
            )
            logger.debug(
                "fetched test records",
                extra={
                    "db.name": DB_NAME,
                    "db.table": TestItem.__tablename__,
                    "db.row_count": len(items),
                },
            )
            return [item.to_dict() for item in items]
    except Exception:
        logger.exception(
            "error fetching test records",
            extra={"db.name": DB_NAME, "db.table": TestItem.__tablename__},
        )
        raise
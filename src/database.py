import os
import random
import string
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

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


# ── Global engines ─────────────────────────────────────────────────────────────

writer_engine: Engine = create_engine(_build_connection_url(DB_WRITER_HOST))
reader_engine: Engine = create_engine(_build_connection_url(DB_READER_HOST))

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

    def __init__(self, *args, mode: str = "write", bind_write=None, bind_read=None, **kwargs):
        if mode not in ("read", "write"):
            raise ValueError(f"mode must be 'read' or 'write', got {mode!r}")
        self._routing_mode = mode
        self._bind_write = bind_write if bind_write is not None else writer_engine
        self._bind_read = bind_read if bind_read is not None else reader_engine
        super().__init__(*args, bind=self._active_bind(), **kwargs)

    def _active_bind(self):
        return self._bind_read if self._routing_mode == "read" else self._bind_write

    def get_bind(self, mapper=None, clause=None, **kwargs):
        bind = self._active_bind()
        logger.debug(
            "routing session bind",
            extra={"db.mode": self._routing_mode, "db.bind": str(bind.url).split("@")[-1]},
        )
        return bind


@contextmanager
def session_scope(mode: str = "write", writer: Engine = None, reader: Engine = None):
    """
    Open a RoutingSession bound to the writer or reader engine for the
    duration of the `with` block, committing on success and rolling back
    (then closing) on error.

    Usage:
        with session_scope(mode="write") as session:
            session.add_all(items)
            session.commit()

        with session_scope(mode="read") as session:
            rows = session.query(TestItem).all()
    """
    session = RoutingSession(
        mode=mode,
        bind_write=writer if writer is not None else writer_engine,
        bind_read=reader if reader is not None else reader_engine,
    )
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _test_engines() -> tuple[Engine, Engine]:
    """Return (writer, reader) engines pointed at the test database."""
    return (
        create_engine(_build_connection_url(DB_WRITER_HOST, db_name=DB_NAME)),
        create_engine(_build_connection_url(DB_READER_HOST, db_name=DB_NAME)),
    )


# ── Main DB functions ──────────────────────────────────────────────────────────

def get_tables() -> list[str]:
    """Fetch all public table names using the reader engine."""
    logger.debug(
        "querying database for tables",
        extra={"db.host": DB_READER_HOST, "db.port": DB_PORT, "db.name": DB_NAME},
    )
    try:
        inspector = inspect(reader_engine)
        tables = sorted(inspector.get_table_names(schema="public"))
        logger.debug("fetched tables", extra={"db.table_count": len(tables)})
        return tables
    except Exception:
        logger.exception("error querying tables", extra={"db.name": DB_NAME})
        raise


# ── Test DB functions ──────────────────────────────────────────────────────────

def create_test_database_and_table() -> None:
    """
    Create the test database (if it does not exist) and sync the ORM schema
    into it via Base.metadata.create_all().

    CREATE DATABASE cannot run inside a transaction block, so a raw
    AUTOCOMMIT connection is used for that one statement only.
    """
    test_writer = create_engine(_build_connection_url(DB_WRITER_HOST, db_name=DB_NAME))
    try:
        Base.metadata.create_all(bind=test_writer)
        logger.info(
            "test schema synced",
            extra={"db.name": DB_NAME, "db.table": TestItem.__tablename__},
        )
    except Exception:
        logger.exception("error syncing test schema", extra={"db.name": DB_NAME})
        raise
    finally:
        test_writer.dispose()


def insert_test_data(n: int = 10) -> list[dict]:
    """
    Insert *n* random TestItem rows into the test database.
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

    test_writer, test_reader = _test_engines()
    try:
        with session_scope(mode="write", writer=test_writer, reader=test_reader) as session:
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
        return [item.to_dict() for item in items]
    except Exception:
        logger.exception(
            "error inserting test data",
            extra={"db.name": DB_NAME, "db.table": TestItem.__tablename__},
        )
        raise
    finally:
        test_writer.dispose()
        test_reader.dispose()


def get_test_records() -> list[dict]:
    """
    Fetch all TestItem rows ordered by created_at descending.
    Always routed to the reader (mode="read") — never guessed.
    """
    test_writer, test_reader = _test_engines()
    try:
        with session_scope(mode="read", writer=test_writer, reader=test_reader) as session:
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
    finally:
        test_writer.dispose()
        test_reader.dispose()
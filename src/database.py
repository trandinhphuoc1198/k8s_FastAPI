import os
import random
import string
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Delete, Insert, Update

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

TEST_DB_NAME = "test_db"

# ── Helpers ────────────────────────────────────────────────────────────────────

def _build_connection_url(host: str, db_name: str = DB_NAME) -> str:
    """Build a SQLAlchemy connection string for the given host and database."""
    return f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{host}:{DB_PORT}/{db_name}"


def _random_string(length: int = 12) -> str:
    """Generate a random alphanumeric string."""
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


# ── Global engines ─────────────────────────────────────────────────────────────

writer_engine: Engine = create_engine(_build_connection_url(DB_WRITER_HOST))
reader_engine: Engine = create_engine(_build_connection_url(DB_READER_HOST))

# ── Read/Write routing ─────────────────────────────────────────────────────────

_WRITE_CLAUSE_TYPES = (Insert, Update, Delete)


class RoutingSession(Session):
    """
    A SQLAlchemy Session that automatically routes queries to the correct engine:
      - INSERT / UPDATE / DELETE  →  writer_engine
      - SELECT (and everything else)  →  reader_engine

    Usage is identical to a regular Session — callers never choose an engine.
    """

    def get_bind(self, mapper=None, clause=None, **kwargs):
        if clause is not None and isinstance(clause, _WRITE_CLAUSE_TYPES):
            logger.debug("routing query to writer", extra={"db.host": DB_WRITER_HOST})
            return writer_engine
        logger.debug("routing query to reader", extra={"db.host": DB_READER_HOST})
        return reader_engine


SessionFactory: sessionmaker[RoutingSession] = sessionmaker(
    class_=RoutingSession,
    expire_on_commit=False,
)


def _make_test_session_factory(db_name: str = TEST_DB_NAME) -> sessionmaker:
    """
    Build a RoutingSession factory pointed at a specific database.
    Creates short-lived engines that are disposed when the session closes.
    """
    test_writer = create_engine(_build_connection_url(DB_WRITER_HOST, db_name=db_name))
    test_reader = create_engine(_build_connection_url(DB_READER_HOST, db_name=db_name))

    class TestRoutingSession(RoutingSession):
        def get_bind(self, mapper=None, clause=None, **kwargs):
            if clause is not None and isinstance(clause, _WRITE_CLAUSE_TYPES):
                logger.debug("routing test query to writer", extra={"db.host": DB_WRITER_HOST})
                return test_writer
            logger.debug("routing test query to reader", extra={"db.host": DB_READER_HOST})
            return test_reader

        def close(self):
            super().close()
            test_writer.dispose()
            test_reader.dispose()

    return sessionmaker(class_=TestRoutingSession, expire_on_commit=False)


# ── Main DB functions ──────────────────────────────────────────────────────────

def get_tables() -> list[str]:
    """Fetch all public table names, routed automatically to the reader."""
    logger.debug(
        "querying database for tables",
        extra={"db.host": DB_READER_HOST, "db.port": DB_PORT, "db.name": DB_NAME},
    )
    try:
        # inspect() is a low-level reflection call — use reader_engine directly
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
    Create the test database (if it does not exist) and sync the ORM schema into it.
    CREATE DATABASE must run outside a transaction, so we use a raw AUTOCOMMIT
    connection here — all other operations go through RoutingSession as normal.
    """
    with writer_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :db"),
            {"db": TEST_DB_NAME},
        ).scalar()

        if not exists:
            conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
            logger.info("test database created", extra={"db.name": TEST_DB_NAME})
        else:
            logger.debug("test database already exists", extra={"db.name": TEST_DB_NAME})

    # Schema sync via ORM — no raw DDL needed
    test_writer = create_engine(_build_connection_url(DB_WRITER_HOST, db_name=TEST_DB_NAME))
    try:
        Base.metadata.create_all(bind=test_writer)
        logger.info(
            "test schema synced",
            extra={"db.name": TEST_DB_NAME, "db.table": TestItem.__tablename__},
        )
    except Exception:
        logger.exception("error syncing test schema", extra={"db.name": TEST_DB_NAME})
        raise
    finally:
        test_writer.dispose()


def insert_test_data(n: int = 10) -> list[dict]:
    """
    Insert *n* random TestItem rows.
    Writes are automatically routed to the writer via TestRoutingSession.
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

    TestSession = _make_test_session_factory()
    try:
        with TestSession() as session:
            session.add_all(items)
            session.commit()
            logger.info(
                "inserted test data",
                extra={
                    "db.name": TEST_DB_NAME,
                    "db.table": TestItem.__tablename__,
                    "db.row_count": n,
                },
            )
        return [item.to_dict() for item in items]
    except Exception:
        logger.exception(
            "error inserting test data",
            extra={"db.name": TEST_DB_NAME, "db.table": TestItem.__tablename__},
        )
        raise


def get_test_records() -> list[dict]:
    """
    Fetch all TestItem rows ordered by created_at descending.
    Reads are automatically routed to the reader via TestRoutingSession.
    """
    TestSession = _make_test_session_factory()
    try:
        with TestSession() as session:
            items = (
                session.query(TestItem)
                .order_by(TestItem.created_at.desc())
                .all()
            )
            logger.debug(
                "fetched test records",
                extra={
                    "db.name": TEST_DB_NAME,
                    "db.table": TestItem.__tablename__,
                    "db.row_count": len(items),
                },
            )
            return [item.to_dict() for item in items]
    except Exception:
        logger.exception(
            "error fetching test records",
            extra={"db.name": TEST_DB_NAME, "db.table": TestItem.__tablename__},
        )
        raise
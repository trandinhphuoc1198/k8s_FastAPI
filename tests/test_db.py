"""
Unit tests for src/database.py.

All database I/O is mocked — no real Postgres connection needed, and
importing this module never requires psycopg2 (or any driver) to be
installed, because engines in database.py are created lazily on first use,
not at import time.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine

from src.database import (
    RoutingSession,
    _build_connection_url,
    _get_reader_engine,
    _get_writer_engine,
    create_test_table,
    get_tables,
    get_test_records,
    insert_test_data,
    session_scope,
)
from src.models import TestItem


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_item(name: str = "foo", value: int = 42) -> TestItem:
    return TestItem(
        id=uuid.uuid4(),
        name=name,
        value=value,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _make_session_ctx(session: MagicMock) -> MagicMock:
    """Wrap a mock session so it works as a context manager."""
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    return session


# ── DB_DRIVER / connection URL ─────────────────────────────────────────────────


def test_build_connection_url_uses_db_driver_env_var():
    with patch("src.database.DB_DRIVER", "psycopg"):
        url = _build_connection_url("localhost")
    assert "postgresql+psycopg://" in url


def test_build_connection_url_includes_host_and_db_name():
    with patch("src.database.DB_DRIVER", "postgresql"):
        url = _build_connection_url("my-host", db_name="mydb")
    assert "my-host" in url
    assert "mydb" in url


# ── Lazy engine creation ────────────────────────────────────────────────────────


def test_get_writer_engine_does_not_require_real_driver_to_be_installed():
    """
    Regression test: importing src.database must never fail with
    NoSuchModuleError, even if a real Postgres driver isn't installed,
    because engines are created lazily on first use rather than at import
    time. This test simulates that by pointing the connection URL builder
    at SQLite (stdlib, always available) instead of mocking anything about
    driver resolution itself.
    """
    with patch("src.database._build_connection_url", return_value="sqlite:///:memory:"), \
         patch("src.database._writer_engine", None):
        engine = _get_writer_engine()
    assert engine is not None
    engine.dispose()


def test_get_writer_engine_is_cached_across_calls():
    fake_engine = MagicMock()
    with patch("src.database.create_engine", return_value=fake_engine), \
         patch("src.database._writer_engine", None):
        first = _get_writer_engine()
        second = _get_writer_engine()
    assert first is second


def test_get_reader_engine_is_cached_across_calls():
    fake_engine = MagicMock()
    with patch("src.database.create_engine", return_value=fake_engine), \
         patch("src.database._reader_engine", None):
        first = _get_reader_engine()
        second = _get_reader_engine()
    assert first is second


# ── RoutingSession: explicit mode, no clause sniffing ─────────────────────────


def test_routing_session_rejects_invalid_mode():
    with pytest.raises(ValueError, match="mode must be"):
        RoutingSession(mode="banana", bind_write=MagicMock(), bind_read=MagicMock())


def test_routing_session_write_mode_binds_to_writer():
    custom_writer, custom_reader = MagicMock(), MagicMock()
    session = RoutingSession(mode="write", bind_write=custom_writer, bind_read=custom_reader)
    assert session.get_bind() is custom_writer
    session.close()


def test_routing_session_read_mode_binds_to_reader():
    custom_writer, custom_reader = MagicMock(), MagicMock()
    session = RoutingSession(mode="read", bind_write=custom_writer, bind_read=custom_reader)
    assert session.get_bind() is custom_reader
    session.close()


def test_routing_session_ignores_clause_argument():
    """
    Regression test for the bug where INSERTs were routed to the reader.

    The bind must depend ONLY on the explicit `mode` passed at construction —
    never on the `clause` or `mapper` arguments SQLAlchemy passes internally
    during ORM flush operations.
    """
    custom_writer, custom_reader = MagicMock(), MagicMock()

    write_session = RoutingSession(mode="write", bind_write=custom_writer, bind_read=custom_reader)
    assert write_session.get_bind(mapper=object(), clause=None) is custom_writer
    write_session.close()

    read_session = RoutingSession(mode="read", bind_write=custom_writer, bind_read=custom_reader)
    fake_clause = MagicMock()
    assert read_session.get_bind(mapper=object(), clause=fake_clause) is custom_reader
    read_session.close()


def test_routing_session_defaults_expire_on_commit_to_false():
    """
    Regression test for: "Instance <TestItem ...> is not bound to a Session;
    attribute refresh operation cannot proceed".

    RoutingSession must default expire_on_commit=False so callers can safely
    read attributes (e.g. for serialization) right after commit.
    """
    session = RoutingSession(mode="write", bind_write=MagicMock(), bind_read=MagicMock())
    assert session.expire_on_commit is False
    session.close()


def test_insert_style_flow_attributes_survive_session_close_with_real_engine():
    """
    End-to-end regression test using a real SQLite in-memory engine (no mocks
    on the session itself) to verify that ORM object attributes are still
    readable right after commit, in the same session block — this is exactly
    the failure mode from the original bug report
    ("Instance ... is not bound to a Session; attribute refresh operation
    cannot proceed").

    Note: TestItem's `id` column uses sqlalchemy.dialects.postgresql.UUID,
    which SQLite cannot compile. This test defines a structurally-equivalent
    local model (string PK instead of UUID) purely so the regression can run
    against a real engine without requiring Postgres in CI.

    Since database.py no longer creates engines at import time, the real
    `sqlalchemy.create_engine` is safe to use directly here — no patching
    of the import-time engine creation is needed.
    """
    from sqlalchemy import Column, Integer as IntCol, String as StrCol
    from sqlalchemy.orm import DeclarativeBase as _DeclarativeBase

    class _LocalBase(_DeclarativeBase):
        pass

    class _LocalItem(_LocalBase):
        __tablename__ = "local_items"
        id = Column(StrCol, primary_key=True)
        name = Column(StrCol, nullable=False)
        value = Column(IntCol, nullable=False)

        def to_dict(self):
            return {"id": self.id, "name": self.name, "value": self.value}

    sqlite_engine = create_engine("sqlite:///:memory:")
    _LocalBase.metadata.create_all(bind=sqlite_engine)

    item = _LocalItem(id=str(uuid.uuid4()), name="regression-check", value=7)

    with session_scope(mode="write", bind_write=sqlite_engine, bind_read=sqlite_engine) as session:
        session.add(item)
        session.commit()
        # Build the dict INSIDE the block, mirroring the fixed
        # insert_test_data. Accessing attributes after commit but outside
        # this block (post session.close()) is what produced the original
        # bug, before expire_on_commit was set to False.
        result = item.to_dict()

    assert result["name"] == "regression-check"
    assert result["value"] == 7

    sqlite_engine.dispose()


# ── session_scope ──────────────────────────────────────────────────────────────


def test_session_scope_write_yields_writer_bound_session():
    custom_writer, custom_reader = MagicMock(), MagicMock()
    with session_scope(mode="write", bind_write=custom_writer, bind_read=custom_reader) as session:
        assert session.get_bind() is custom_writer


def test_session_scope_read_yields_reader_bound_session():
    custom_writer, custom_reader = MagicMock(), MagicMock()
    with session_scope(mode="read", bind_write=custom_writer, bind_read=custom_reader) as session:
        assert session.get_bind() is custom_reader


def test_session_scope_rolls_back_and_reraises_on_error():
    custom_writer, custom_reader = MagicMock(), MagicMock()
    with pytest.raises(ValueError, match="boom"):
        with session_scope(mode="write", bind_write=custom_writer, bind_read=custom_reader) as session:
            session.rollback = MagicMock()
            raise ValueError("boom")


def test_session_scope_closes_session_on_success():
    custom_writer, custom_reader = MagicMock(), MagicMock()
    captured = {}

    with session_scope(mode="read", bind_write=custom_writer, bind_read=custom_reader) as session:
        captured["session"] = session

    captured["session"].close()  # idempotent, should not raise


# ── get_tables ─────────────────────────────────────────────────────────────────


def test_get_tables_returns_sorted_table_names():
    mock_inspector = MagicMock()
    mock_inspector.get_table_names.return_value = ["users", "orders", "products"]

    with patch("src.database.inspect", return_value=mock_inspector), \
         patch("src.database._get_reader_engine", return_value=MagicMock()):
        result = get_tables()

    assert result == ["orders", "products", "users"]


def test_get_tables_returns_empty_list_when_no_tables():
    mock_inspector = MagicMock()
    mock_inspector.get_table_names.return_value = []

    with patch("src.database.inspect", return_value=mock_inspector), \
         patch("src.database._get_reader_engine", return_value=MagicMock()):
        result = get_tables()

    assert result == []


def test_get_tables_uses_reader_engine():
    mock_inspector = MagicMock()
    mock_inspector.get_table_names.return_value = []
    mock_reader = MagicMock()

    with patch("src.database.inspect", return_value=mock_inspector) as mock_inspect, \
         patch("src.database._get_reader_engine", return_value=mock_reader):
        get_tables()

    mock_inspect.assert_called_once_with(mock_reader)


def test_get_tables_raises_on_db_error():
    mock_inspector = MagicMock()
    mock_inspector.get_table_names.side_effect = Exception("connection refused")

    with patch("src.database.inspect", return_value=mock_inspector), \
         patch("src.database._get_reader_engine", return_value=MagicMock()):
        with pytest.raises(Exception, match="connection refused"):
            get_tables()


# ── create_test_table ──────────────────────────────────────────────────────────


def test_create_test_table_calls_create_all_with_writer_engine():
    mock_writer = MagicMock()
    with patch("src.database._get_writer_engine", return_value=mock_writer), \
         patch("src.database.Base.metadata.create_all") as mock_create_all:
        create_test_table()

    mock_create_all.assert_called_once_with(bind=mock_writer)


def test_create_test_table_raises_on_error():
    with patch("src.database._get_writer_engine", return_value=MagicMock()), \
         patch("src.database.Base.metadata.create_all", side_effect=Exception("permission denied")):
        with pytest.raises(Exception, match="permission denied"):
            create_test_table()


# ── insert_test_data ──────────────────────────────────────────────────────────


def test_insert_test_data_raises_for_n_less_than_1():
    with pytest.raises(ValueError, match="n must be >= 1"):
        insert_test_data(n=0)


def test_insert_test_data_uses_write_mode_session():
    mock_session = _make_session_ctx(MagicMock())

    with patch("src.database.session_scope") as mock_scope:
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        insert_test_data(n=5)

    args = mock_scope.call_args
    mode = args.kwargs.get("mode") or (args.args[0] if args.args else None)
    assert mode == "write"


def test_insert_test_data_adds_correct_number_of_items():
    mock_session = _make_session_ctx(MagicMock())

    with patch("src.database.session_scope") as mock_scope:
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        insert_test_data(n=5)

    added = mock_session.add_all.call_args[0][0]
    assert len(added) == 5
    assert all(isinstance(item, TestItem) for item in added)


def test_insert_test_data_commits_session():
    mock_session = _make_session_ctx(MagicMock())

    with patch("src.database.session_scope") as mock_scope:
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        insert_test_data(n=3)

    mock_session.commit.assert_called_once()


def test_insert_test_data_returns_list_of_dicts():
    mock_session = _make_session_ctx(MagicMock())

    with patch("src.database.session_scope") as mock_scope:
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        result = insert_test_data(n=3)

    assert isinstance(result, list)
    assert len(result) == 3
    for row in result:
        assert set(row.keys()) == {"id", "name", "value", "created_at"}


def test_insert_test_data_raises_and_propagates_on_commit_error():
    mock_session = _make_session_ctx(MagicMock())
    mock_session.commit.side_effect = Exception("deadlock detected")

    with patch("src.database.session_scope") as mock_scope:
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(Exception, match="deadlock detected"):
            insert_test_data(n=1)


# ── get_test_records ──────────────────────────────────────────────────────────


def test_get_test_records_uses_read_mode_session():
    mock_session = _make_session_ctx(MagicMock())
    mock_session.query.return_value.order_by.return_value.all.return_value = []

    with patch("src.database.session_scope") as mock_scope:
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        get_test_records()

    args = mock_scope.call_args
    mode = args.kwargs.get("mode") or (args.args[0] if args.args else None)
    assert mode == "read"


def test_get_test_records_returns_list_of_dicts():
    items = [_make_item("alpha", 1), _make_item("beta", 2)]
    mock_session = _make_session_ctx(MagicMock())
    mock_session.query.return_value.order_by.return_value.all.return_value = items

    with patch("src.database.session_scope") as mock_scope:
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        result = get_test_records()

    assert len(result) == 2
    assert result[0]["name"] == "alpha"
    assert result[1]["name"] == "beta"


def test_get_test_records_queries_test_item_model():
    mock_session = _make_session_ctx(MagicMock())
    mock_session.query.return_value.order_by.return_value.all.return_value = []

    with patch("src.database.session_scope") as mock_scope:
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        get_test_records()

    mock_session.query.assert_called_once_with(TestItem)


def test_get_test_records_returns_empty_list_when_no_rows():
    mock_session = _make_session_ctx(MagicMock())
    mock_session.query.return_value.order_by.return_value.all.return_value = []

    with patch("src.database.session_scope") as mock_scope:
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        result = get_test_records()

    assert result == []


def test_get_test_records_raises_and_propagates_on_query_error():
    mock_session = _make_session_ctx(MagicMock())
    mock_session.query.side_effect = Exception("connection timeout")

    with patch("src.database.session_scope") as mock_scope:
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(Exception, match="connection timeout"):
            get_test_records()
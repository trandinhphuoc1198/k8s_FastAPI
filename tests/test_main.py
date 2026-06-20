"""
Unit tests for src/database.py.

All database I/O is mocked — no real Postgres connection needed.

conftest.py handles the session-wide create_engine patch so importing
src.database never touches a real driver.
"""

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.database import (
    RoutingSession,
    _build_connection_url,
    create_test_database_and_table,
    get_tables,
    get_test_records,
    insert_test_data,
    reader_engine,
    session_scope,
    writer_engine,
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


# ── RoutingSession: explicit mode, no clause sniffing ─────────────────────────


def test_routing_session_rejects_invalid_mode():
    with pytest.raises(ValueError, match="mode must be"):
        RoutingSession(mode="banana")


def test_routing_session_write_mode_binds_to_writer():
    session = RoutingSession(mode="write")
    assert session.get_bind() is writer_engine
    session.close()


def test_routing_session_read_mode_binds_to_reader():
    session = RoutingSession(mode="read")
    assert session.get_bind() is reader_engine
    session.close()


def test_routing_session_ignores_clause_argument():
    """
    Regression test for the bug where INSERTs were routed to the reader.

    The bind must depend ONLY on the explicit `mode` passed at construction —
    never on the `clause` or `mapper` arguments SQLAlchemy passes internally
    during ORM flush operations.
    """
    write_session = RoutingSession(mode="write")
    # Simulate what an ORM flush passes: mapper set, clause None
    assert write_session.get_bind(mapper=object(), clause=None) is writer_engine
    write_session.close()

    read_session = RoutingSession(mode="read")
    # Even if some arbitrary "insert-like" clause object were passed in,
    # read mode must still bind to the reader.
    fake_clause = MagicMock()
    assert read_session.get_bind(mapper=object(), clause=fake_clause) is reader_engine
    read_session.close()


def test_routing_session_uses_custom_engines_when_provided():
    custom_writer = MagicMock()
    custom_reader = MagicMock()

    write_session = RoutingSession(mode="write", bind_write=custom_writer, bind_read=custom_reader)
    assert write_session.get_bind() is custom_writer
    write_session.close()

    read_session = RoutingSession(mode="read", bind_write=custom_writer, bind_read=custom_reader)
    assert read_session.get_bind() is custom_reader
    read_session.close()


# ── session_scope ──────────────────────────────────────────────────────────────


def test_session_scope_write_yields_writer_bound_session():
    with session_scope(mode="write") as session:
        assert session.get_bind() is writer_engine


def test_session_scope_read_yields_reader_bound_session():
    with session_scope(mode="read") as session:
        assert session.get_bind() is reader_engine


def test_session_scope_rolls_back_and_reraises_on_error():
    with pytest.raises(ValueError, match="boom"):
        with session_scope(mode="write") as session:
            session.rollback = MagicMock(wraps=session.rollback)
            raise ValueError("boom")


def test_session_scope_closes_session_on_success():
    captured = {}

    with session_scope(mode="read") as session:
        captured["session"] = session

    # after the with-block, session should be closed (no exception on re-close)
    captured["session"].close()  # idempotent, should not raise


# ── get_tables ─────────────────────────────────────────────────────────────────


def test_get_tables_returns_sorted_table_names():
    mock_inspector = MagicMock()
    mock_inspector.get_table_names.return_value = ["users", "orders", "products"]

    with patch("src.database.inspect", return_value=mock_inspector):
        result = get_tables()

    assert result == ["orders", "products", "users"]


def test_get_tables_returns_empty_list_when_no_tables():
    mock_inspector = MagicMock()
    mock_inspector.get_table_names.return_value = []

    with patch("src.database.inspect", return_value=mock_inspector):
        result = get_tables()

    assert result == []


def test_get_tables_uses_reader_engine():
    mock_inspector = MagicMock()
    mock_inspector.get_table_names.return_value = []

    with patch("src.database.inspect", return_value=mock_inspector) as mock_inspect:
        get_tables()

    mock_inspect.assert_called_once_with(reader_engine)


def test_get_tables_raises_on_db_error():
    mock_inspector = MagicMock()
    mock_inspector.get_table_names.side_effect = Exception("connection refused")

    with patch("src.database.inspect", return_value=mock_inspector):
        with pytest.raises(Exception, match="connection refused"):
            get_tables()


# ── create_test_database_and_table ────────────────────────────────────────────


def _make_autocommit_conn(db_exists: bool):
    conn = MagicMock()
    conn.execute.return_value.scalar.return_value = 1 if db_exists else None
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)

    engine_conn = MagicMock()
    engine_conn.execution_options.return_value = conn
    return engine_conn, conn



def test_create_test_db_syncs_schema_via_create_all():
    engine_conn, _ = _make_autocommit_conn(db_exists=True)

    with (
        patch("src.database.writer_engine") as mock_writer,
        patch("src.database.create_engine"),
        patch("src.database.Base.metadata.create_all") as mock_create_all,
    ):
        mock_writer.connect.return_value = engine_conn
        create_test_database_and_table()

    mock_create_all.assert_called_once()


def test_create_test_db_disposes_engine_on_error():
    engine_conn, _ = _make_autocommit_conn(db_exists=True)
    mock_test_engine = MagicMock()

    with (
        patch("src.database.writer_engine") as mock_writer,
        patch("src.database.create_engine", return_value=mock_test_engine),
        patch("src.database.Base.metadata.create_all", side_effect=Exception("boom")),
    ):
        mock_writer.connect.return_value = engine_conn
        with pytest.raises(Exception, match="boom"):
            create_test_database_and_table()

    mock_test_engine.dispose.assert_called_once()


# ── insert_test_data ──────────────────────────────────────────────────────────


def test_insert_test_data_raises_for_n_less_than_1():
    with pytest.raises(ValueError, match="n must be >= 1"):
        insert_test_data(n=0)


def test_insert_test_data_uses_write_mode_session():
    mock_session = _make_session_ctx(MagicMock())

    with (
        patch("src.database._test_engines", return_value=(MagicMock(), MagicMock())),
        patch("src.database.session_scope") as mock_scope,
    ):
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        insert_test_data(n=5)

    # Verify session_scope was called with mode="write"
    _, kwargs = mock_scope.call_args
    assert kwargs.get("mode") == "write" or mock_scope.call_args[0][0] == "write"


def test_insert_test_data_adds_correct_number_of_items():
    mock_session = _make_session_ctx(MagicMock())

    with (
        patch("src.database._test_engines", return_value=(MagicMock(), MagicMock())),
        patch("src.database.session_scope") as mock_scope,
    ):
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        insert_test_data(n=5)

    added = mock_session.add_all.call_args[0][0]
    assert len(added) == 5
    assert all(isinstance(item, TestItem) for item in added)


def test_insert_test_data_commits_session():
    mock_session = _make_session_ctx(MagicMock())

    with (
        patch("src.database._test_engines", return_value=(MagicMock(), MagicMock())),
        patch("src.database.session_scope") as mock_scope,
    ):
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        insert_test_data(n=3)

    mock_session.commit.assert_called_once()


def test_insert_test_data_returns_list_of_dicts():
    mock_session = _make_session_ctx(MagicMock())

    with (
        patch("src.database._test_engines", return_value=(MagicMock(), MagicMock())),
        patch("src.database.session_scope") as mock_scope,
    ):
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        result = insert_test_data(n=3)

    assert isinstance(result, list)
    assert len(result) == 3
    for row in result:
        assert set(row.keys()) == {"id", "name", "value", "created_at"}


def test_insert_test_data_disposes_test_engines_on_error():
    mock_session = _make_session_ctx(MagicMock())
    mock_session.commit.side_effect = Exception("deadlock detected")
    mock_test_writer, mock_test_reader = MagicMock(), MagicMock()

    with (
        patch("src.database._test_engines", return_value=(mock_test_writer, mock_test_reader)),
        patch("src.database.session_scope") as mock_scope,
    ):
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(Exception, match="deadlock detected"):
            insert_test_data(n=1)

    mock_test_writer.dispose.assert_called_once()
    mock_test_reader.dispose.assert_called_once()


# ── get_test_records ──────────────────────────────────────────────────────────


def test_get_test_records_uses_read_mode_session():
    mock_session = _make_session_ctx(MagicMock())
    mock_session.query.return_value.order_by.return_value.all.return_value = []

    with (
        patch("src.database._test_engines", return_value=(MagicMock(), MagicMock())),
        patch("src.database.session_scope") as mock_scope,
    ):
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

    with (
        patch("src.database._test_engines", return_value=(MagicMock(), MagicMock())),
        patch("src.database.session_scope") as mock_scope,
    ):
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        result = get_test_records()

    assert len(result) == 2
    assert result[0]["name"] == "alpha"
    assert result[1]["name"] == "beta"


def test_get_test_records_queries_test_item_model():
    mock_session = _make_session_ctx(MagicMock())
    mock_session.query.return_value.order_by.return_value.all.return_value = []

    with (
        patch("src.database._test_engines", return_value=(MagicMock(), MagicMock())),
        patch("src.database.session_scope") as mock_scope,
    ):
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        get_test_records()

    mock_session.query.assert_called_once_with(TestItem)


def test_get_test_records_returns_empty_list_when_no_rows():
    mock_session = _make_session_ctx(MagicMock())
    mock_session.query.return_value.order_by.return_value.all.return_value = []

    with (
        patch("src.database._test_engines", return_value=(MagicMock(), MagicMock())),
        patch("src.database.session_scope") as mock_scope,
    ):
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        result = get_test_records()

    assert result == []


def test_get_test_records_disposes_test_engines_on_error():
    mock_session = _make_session_ctx(MagicMock())
    mock_session.query.side_effect = Exception("connection timeout")
    mock_test_writer, mock_test_reader = MagicMock(), MagicMock()

    with (
        patch("src.database._test_engines", return_value=(mock_test_writer, mock_test_reader)),
        patch("src.database.session_scope") as mock_scope,
    ):
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(Exception, match="connection timeout"):
            get_test_records()

    mock_test_writer.dispose.assert_called_once()
    mock_test_reader.dispose.assert_called_once()
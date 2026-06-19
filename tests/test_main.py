"""
Unit tests for src/database.py.

All database I/O is mocked — no real Postgres connection needed.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest
from sqlalchemy.sql import Delete, Insert, Select, Update, select

from src.database import (
    RoutingSession,
    SessionFactory,
    create_test_database_and_table,
    get_tables,
    get_test_records,
    insert_test_data,
    reader_engine,
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


# ── RoutingSession ─────────────────────────────────────────────────────────────


def test_routing_session_routes_select_to_reader():
    session = RoutingSession()
    clause = select(TestItem)
    result = session.get_bind(clause=clause)
    assert result is reader_engine


def test_routing_session_routes_insert_to_writer():
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    session = RoutingSession()
    clause = Insert(TestItem.__table__)
    result = session.get_bind(clause=clause)
    assert result is writer_engine


def test_routing_session_routes_update_to_writer():
    session = RoutingSession()
    clause = Update(TestItem.__table__)
    result = session.get_bind(clause=clause)
    assert result is writer_engine


def test_routing_session_routes_delete_to_writer():
    session = RoutingSession()
    clause = Delete(TestItem.__table__)
    result = session.get_bind(clause=clause)
    assert result is writer_engine


def test_routing_session_routes_none_clause_to_reader():
    """When clause is None (e.g. session.flush()), default to reader."""
    session = RoutingSession()
    result = session.get_bind(clause=None)
    assert result is reader_engine


def test_session_factory_produces_routing_session():
    session = SessionFactory()
    assert isinstance(session, RoutingSession)
    session.close()


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


def test_get_tables_queries_public_schema():
    mock_inspector = MagicMock()
    mock_inspector.get_table_names.return_value = []

    with patch("src.database.inspect", return_value=mock_inspector):
        get_tables()

    mock_inspector.get_table_names.assert_called_once_with(schema="public")


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


def _make_autocommit_conn(db_exists: bool) -> MagicMock:
    conn = MagicMock()
    conn.execute.return_value.scalar.return_value = 1 if db_exists else None
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)

    engine_conn = MagicMock()
    engine_conn.execution_options.return_value = conn
    return engine_conn, conn



def test_create_test_db_skips_create_database_when_exists():
    engine_conn, conn = _make_autocommit_conn(db_exists=True)

    with (
        patch("src.database.writer_engine") as mock_writer,
        patch("src.database.create_engine"),
        patch("src.database.Base.metadata.create_all"),
    ):
        mock_writer.connect.return_value = engine_conn
        create_test_database_and_table()

    executed = [str(c.args[0]) for c in conn.execute.call_args_list]
    assert not any("CREATE DATABASE" in s for s in executed)


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


def test_insert_test_data_adds_correct_number_of_items():
    mock_session = _make_session_ctx(MagicMock())
    mock_factory = MagicMock(return_value=mock_session)

    with (
        patch("src.database.create_engine"),
        patch("src.database._make_test_session_factory", return_value=mock_factory),
    ):
        insert_test_data(n=5)

    added = mock_session.add_all.call_args[0][0]
    assert len(added) == 5


def test_insert_test_data_all_items_are_test_item_instances():
    mock_session = _make_session_ctx(MagicMock())
    mock_factory = MagicMock(return_value=mock_session)

    with (
        patch("src.database.create_engine"),
        patch("src.database._make_test_session_factory", return_value=mock_factory),
    ):
        insert_test_data(n=3)

    added = mock_session.add_all.call_args[0][0]
    assert all(isinstance(item, TestItem) for item in added)


def test_insert_test_data_commits_session():
    mock_session = _make_session_ctx(MagicMock())
    mock_factory = MagicMock(return_value=mock_session)

    with (
        patch("src.database.create_engine"),
        patch("src.database._make_test_session_factory", return_value=mock_factory),
    ):
        insert_test_data(n=3)

    mock_session.commit.assert_called_once()


def test_insert_test_data_returns_list_of_dicts():
    mock_session = _make_session_ctx(MagicMock())
    mock_factory = MagicMock(return_value=mock_session)

    with (
        patch("src.database.create_engine"),
        patch("src.database._make_test_session_factory", return_value=mock_factory),
    ):
        result = insert_test_data(n=3)

    assert isinstance(result, list)
    assert len(result) == 3
    for row in result:
        assert set(row.keys()) == {"id", "name", "value", "created_at"}


def test_insert_test_data_raises_and_propagates_on_commit_error():
    mock_session = _make_session_ctx(MagicMock())
    mock_session.commit.side_effect = Exception("deadlock detected")
    mock_factory = MagicMock(return_value=mock_session)

    with (
        patch("src.database.create_engine"),
        patch("src.database._make_test_session_factory", return_value=mock_factory),
    ):
        with pytest.raises(Exception, match="deadlock detected"):
            insert_test_data(n=1)


# ── get_test_records ──────────────────────────────────────────────────────────


def test_get_test_records_returns_list_of_dicts():
    items = [_make_item("alpha", 1), _make_item("beta", 2)]
    mock_session = _make_session_ctx(MagicMock())
    mock_session.query.return_value.order_by.return_value.all.return_value = items
    mock_factory = MagicMock(return_value=mock_session)

    with (
        patch("src.database.create_engine"),
        patch("src.database._make_test_session_factory", return_value=mock_factory),
    ):
        result = get_test_records()

    assert len(result) == 2
    assert result[0]["name"] == "alpha"
    assert result[1]["name"] == "beta"


def test_get_test_records_queries_test_item_model():
    mock_session = _make_session_ctx(MagicMock())
    mock_session.query.return_value.order_by.return_value.all.return_value = []
    mock_factory = MagicMock(return_value=mock_session)

    with (
        patch("src.database.create_engine"),
        patch("src.database._make_test_session_factory", return_value=mock_factory),
    ):
        get_test_records()

    mock_session.query.assert_called_once_with(TestItem)


def test_get_test_records_applies_order_by():
    mock_session = _make_session_ctx(MagicMock())
    query_mock = mock_session.query.return_value
    query_mock.order_by.return_value.all.return_value = []
    mock_factory = MagicMock(return_value=mock_session)

    with (
        patch("src.database.create_engine"),
        patch("src.database._make_test_session_factory", return_value=mock_factory),
    ):
        get_test_records()

    query_mock.order_by.assert_called_once()


def test_get_test_records_returns_empty_list_when_no_rows():
    mock_session = _make_session_ctx(MagicMock())
    mock_session.query.return_value.order_by.return_value.all.return_value = []
    mock_factory = MagicMock(return_value=mock_session)

    with (
        patch("src.database.create_engine"),
        patch("src.database._make_test_session_factory", return_value=mock_factory),
    ):
        result = get_test_records()

    assert result == []


def test_get_test_records_raises_and_propagates_on_query_error():
    mock_session = _make_session_ctx(MagicMock())
    mock_session.query.side_effect = Exception("connection timeout")
    mock_factory = MagicMock(return_value=mock_session)

    with (
        patch("src.database.create_engine"),
        patch("src.database._make_test_session_factory", return_value=mock_factory),
    ):
        with pytest.raises(Exception, match="connection timeout"):
            get_test_records()
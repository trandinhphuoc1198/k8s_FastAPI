"""
conftest.py — session-scoped fixtures that prevent any real DB driver from
being loaded when src.database is imported during tests.

Problem: database.py calls create_engine() at module level (to build the
global writer_engine / reader_engine). That triggers SQLAlchemy to load the
driver named in the connection URL (psycopg2 by default), which may not be
installed in the test environment.

Solution: patch create_engine before the module is first imported, and set
DB_DRIVER to the driverless "postgresql" dialect so the URL itself is safe.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy

# Capture the REAL create_engine before the autouse fixture below patches it.
# Tests that need a genuine engine (e.g. SQLite in-memory for true ORM
# behavior checks) should import `real_create_engine` from this module
# rather than calling sqlalchemy.create_engine directly, since the latter
# is patched for the entire test session.
real_create_engine = sqlalchemy.create_engine


# ── Environment ────────────────────────────────────────────────────────────────
# Set before any src.* import so _build_connection_url picks it up.
# "postgresql" (no driver suffix) is a valid SQLAlchemy dialect string that
# does not require any external package to be installed.
os.environ.setdefault("DB_DRIVER", "postgresql")


# ── Module-level engine mock ───────────────────────────────────────────────────

@pytest.fixture(autouse=True, scope="session")
def mock_create_engine():
    """
    Replace sqlalchemy.create_engine for the entire test session.

    This prevents SQLAlchemy from attempting a real driver connection when
    database.py is imported.  Individual tests that need finer-grained engine
    control can still patch src.database.create_engine locally — those patches
    take priority over this session-level one.
    """
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_engine.connect.return_value)
    mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

    with patch("sqlalchemy.create_engine", return_value=mock_engine):
        yield mock_engine
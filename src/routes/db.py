from fastapi import APIRouter, HTTPException, Query

from src.database import (
    create_test_database_and_table,
    get_tables,
    get_test_records,
    insert_test_data,
)
from src.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/db", tags=["database"])


@router.get("/tables")
def list_tables():
    """Return all public tables from the main database."""
    try:
        tables = get_tables()
        return {"database": "TestDb", "tables": tables}
    except Exception as exc:
        logger.error("failed to list tables", extra={"error": str(exc)}, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/test/setup", status_code=201)
def setup_test_db():
    """Create the test database and test table if they do not already exist."""
    try:
        create_test_database_and_table()
        return {"status": "ok", "message": "Test database and table are ready."}
    except Exception as exc:
        logger.error("failed to set up test database", extra={"error": str(exc)}, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/test/seed", status_code=201)
def seed_test_data(
    n: int = Query(default=10, ge=1, le=1000, description="Number of random rows to insert"),
):
    """Insert *n* random rows into the test table (default: 10, max: 1000)."""
    try:
        rows = insert_test_data(n=n)
        return {"status": "ok", "inserted": len(rows), "rows": rows}
    except Exception as exc:
        logger.error("failed to seed test data", extra={"error": str(exc)}, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/test/records")
def list_test_records():
    """Return all rows from the test table, ordered by created_at descending."""
    try:
        records = get_test_records()
        return {"count": len(records), "records": records}
    except Exception as exc:
        logger.error("failed to fetch test records", extra={"error": str(exc)}, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))
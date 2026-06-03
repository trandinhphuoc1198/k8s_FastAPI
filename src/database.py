import os

import psycopg2
from dotenv import load_dotenv

from src.logging import get_logger

load_dotenv()

logger = get_logger(__name__)

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "TestDb")


def get_connection() -> psycopg2.extensions.connection:
    logger.debug(
        "opening database connection",
        extra={"db.host": DB_HOST, "db.port": DB_PORT, "db.name": DB_NAME},
    )
    return psycopg2.connect(
        host=DB_HOST,
        port=int(DB_PORT),
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=DB_NAME,
    )


def get_tables() -> list[str]:
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name;
                """
            )
            rows = [row[0] for row in cursor.fetchall()]
            logger.debug("fetched tables", extra={"db.table_count": len(rows)})
            return rows
    except Exception:
        logger.exception("error querying tables", extra={"db.name": DB_NAME})
        raise
    finally:
        conn.close()
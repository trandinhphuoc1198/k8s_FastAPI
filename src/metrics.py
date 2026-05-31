from prometheus_client import Counter, Histogram, Gauge
import time

# Request metrics
REQUEST_COUNT = Counter(
    "fastapi_request_count_total",
    "Total requests",
    ["method", "endpoint", "status_code"],
)

REQUEST_LATENCY = Histogram(
    "fastapi_request_latency_seconds",
    "Request latency in seconds",
    ["method", "endpoint"],
)

# Application metrics
ACTIVE_REQUESTS = Gauge(
    "fastapi_active_requests",
    "Number of active requests",
)

# Database metrics
DB_QUERY_TIME = Histogram(
    "db_query_time_seconds",
    "Database query execution time in seconds",
    ["query_type"],
)

DB_CONNECTION_POOL_SIZE = Gauge(
    "db_connection_pool_size",
    "Current size of the database connection pool",
)

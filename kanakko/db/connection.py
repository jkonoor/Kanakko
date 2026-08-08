"""The one connection opener, shared by every db module and the jobs."""

import os

import psycopg


def connect() -> psycopg.Connection:
    """Open a connection from `DATABASE_URL`. Fails closed if it is unset."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(dsn)

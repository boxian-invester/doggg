"""PostgreSQL helpers. DATABASE_URL is server-only and must never be sent to browser."""
import os
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row

def configured():
    return bool(os.getenv('DATABASE_URL'))

@contextmanager
def connect():
    url=os.getenv('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL 未設定')
    # Supabase pooler/transaction connections typically require TLS.
    with psycopg.connect(url, connect_timeout=20, row_factory=dict_row) as conn:
        yield conn

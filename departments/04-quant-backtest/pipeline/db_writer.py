"""Write-capable Supabase connections safe for transaction pooling.

Supabase port 6543 is a transaction pooler.  A client that changes
``default_transaction_read_only`` at session scope can leave that default on a
server connection which is later handed to a writer.  psycopg's
``set_session(readonly=False)`` records READ WRITE as a transaction
characteristic, so every subsequent transaction starts with the intended mode
without changing the pooled server's session default.
"""

from __future__ import annotations


def connect(dsn: str, *, connect_timeout: int = 20):
    import psycopg2

    conn = psycopg2.connect(dsn, connect_timeout=connect_timeout)
    try:
        conn.set_session(readonly=False)
    except Exception:
        conn.close()
        raise
    return conn

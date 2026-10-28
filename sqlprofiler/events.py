"""Data exchanged between the interceptor and the rest of the pipeline."""
import itertools
import time
from dataclasses import dataclass, field

_conn_ids = itertools.count(1)


@dataclass
class ConnectionInfo:
    """One client connection accepted by a proxy."""
    proxy: str                      # proxy name, e.g. "mysql:3307"
    protocol: str                   # "mysql" | "postgres"
    client: str                     # "ip:port" of the target application
    id: int = field(default_factory=lambda: next(_conn_ids))
    user: str = ""
    database: str = ""
    opened_at: float = field(default_factory=time.time)
    closed_at: float | None = None
    queries: int = 0
    encrypted: bool = False         # TLS could not be avoided: traffic is forwarded but not decoded
    note: str = ""

    def as_dict(self):
        return {"id": self.id, "proxy": self.proxy, "protocol": self.protocol, "client": self.client,
                "user": self.user, "database": self.database, "opened_at": self.opened_at,
                "closed_at": self.closed_at, "queries": self.queries, "encrypted": self.encrypted,
                "note": self.note}


@dataclass
class CapturedQuery:
    """A statement seen on the wire, with the outcome reported by the server."""
    conn: ConnectionInfo
    sql: str                        # statement text as sent (placeholders kept for prepared statements)
    kind: str = "query"             # "query" (text protocol) | "execute" (prepared/extended) | "prepare" | "use"
    params: list | None = None      # bound parameter values for prepared statements
    started_at: float = 0.0         # wall clock (epoch seconds) when the command left the client
    duration_ms: float = 0.0        # from command sent to last response packet received
    rows: int | None = None         # rows returned by a result set
    affected: int | None = None     # rows affected by DML
    error: str | None = None        # server error, e.g. "1146 (42S02): Table 'x' doesn't exist"
    truncated: bool = False         # statement longer than the capture limit

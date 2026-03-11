import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator


class LocalDB:
    """Small SQLite helper used by runtime state machine."""

    def __init__(self, db_path: str = "workspace/state/runtime.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


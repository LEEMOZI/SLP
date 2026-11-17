import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value):
        if isinstance(value, str):
            return " ".join(value.split())
        return value


class Case(BaseModel):
    id: str
    name: str
    created_at: str
    updated_at: str
    last_opened_at: Optional[str] = None
    archived_at: Optional[str] = None


class CaseList(BaseModel):
    items: List[Case]
    total: int


class CaseConflictError(Exception):
    pass


class CaseArchivedError(Exception):
    pass


class CaseRepository:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(str(self.db_path), timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_opened_at TEXT,
                    archived_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_cases_updated_at
                    ON cases(updated_at DESC);
                """
            )

    @staticmethod
    def to_case(row: sqlite3.Row) -> Case:
        return Case(**dict(row))

    def create(self, name: str) -> Case:
        case_id = str(uuid.uuid4())
        now = utc_now()
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO cases (id, name, normalized_name, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (case_id, name, name.casefold(), now, now),
                )
                row = connection.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        except sqlite3.IntegrityError as error:
            raise CaseConflictError(name) from error
        return self.to_case(row)

    def list(self, include_archived: bool = False) -> List[Case]:
        query = "SELECT * FROM cases"
        if not include_archived:
            query += " WHERE archived_at IS NULL"
        query += " ORDER BY updated_at DESC, created_at DESC"
        with self.connect() as connection:
            rows = connection.execute(query).fetchall()
        return [self.to_case(row) for row in rows]

    def get(self, case_id: str) -> Optional[Case]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        return self.to_case(row) if row else None

    def open(self, case_id: str) -> Optional[Case]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
            if row is None:
                return None
            if row["archived_at"] is not None:
                raise CaseArchivedError(case_id)
            now = utc_now()
            connection.execute(
                "UPDATE cases SET last_opened_at = ?, updated_at = ? WHERE id = ?",
                (now, now, case_id),
            )
            row = connection.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        return self.to_case(row)

    def archive(self, case_id: str) -> Optional[Case]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
            if row is None:
                return None
            if row["archived_at"] is None:
                now = utc_now()
                connection.execute(
                    "UPDATE cases SET archived_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, case_id),
                )
                row = connection.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        return self.to_case(row)

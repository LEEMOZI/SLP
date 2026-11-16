import sqlite3
from typing import Dict


MIGRATION_VERSIONS = {
    1: "task_creation_idempotency",
    2: "material_parse_versions",
    3: "evidence_items_and_documents",
}


def _columns(connection: sqlite3.Connection, table: str) -> Dict[str, sqlite3.Row]:
    return {
        row[1]: row
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def run_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = {
        row[0]
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    if 1 not in applied:
        columns = _columns(connection, "tasks")
        if "client_request_id" not in columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN client_request_id TEXT")
        if "request_hash" not in columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN request_hash TEXT")
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_client_request
            ON tasks(case_id, task_type, client_request_id)
            WHERE client_request_id IS NOT NULL
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
            (1, MIGRATION_VERSIONS[1]),
        )
    if 2 not in applied:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS materials (
                id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL REFERENCES cases(id),
                original_name TEXT NOT NULL,
                format TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                storage_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                current_version_id TEXT,
                latest_task_id TEXT,
                client_request_id TEXT,
                request_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_materials_client_request
            ON materials(case_id, client_request_id)
            WHERE client_request_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_materials_case_created
            ON materials(case_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_materials_case_hash
            ON materials(case_id, sha256);

            CREATE TABLE IF NOT EXISTS material_parse_versions (
                id TEXT PRIMARY KEY,
                material_id TEXT NOT NULL REFERENCES materials(id),
                task_id TEXT NOT NULL REFERENCES tasks(id),
                attempt_id TEXT NOT NULL UNIQUE REFERENCES task_attempts(id),
                parser_version TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                page_count INTEGER,
                segment_count INTEGER NOT NULL DEFAULT 0,
                char_count INTEGER NOT NULL DEFAULT 0,
                locator_type TEXT,
                coverage TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_material_versions_material
            ON material_parse_versions(material_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS material_segments (
                id TEXT PRIMARY KEY,
                parse_version_id TEXT NOT NULL REFERENCES material_parse_versions(id),
                sequence INTEGER NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL,
                locator TEXT NOT NULL DEFAULT '{}',
                extraction_method TEXT,
                error TEXT,
                UNIQUE(parse_version_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_material_segments_version
            ON material_segments(parse_version_id, sequence);
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
            (2, MIGRATION_VERSIONS[2]),
        )
    if 3 not in applied:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS evidence_items (
                id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL REFERENCES cases(id),
                task_id TEXT NOT NULL REFERENCES tasks(id),
                attempt_id TEXT NOT NULL REFERENCES task_attempts(id),
                group_name TEXT NOT NULL,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                source_label TEXT NOT NULL,
                excerpt TEXT NOT NULL,
                content TEXT NOT NULL,
                hit_reason TEXT NOT NULL,
                match_type TEXT NOT NULL,
                partial_match INTEGER NOT NULL DEFAULT 0,
                rank INTEGER NOT NULL,
                score REAL,
                locator TEXT NOT NULL DEFAULT '{}',
                source_ref TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_evidence_attempt
            ON evidence_items(attempt_id, group_name, source, rank);
            CREATE INDEX IF NOT EXISTS idx_evidence_case
            ON evidence_items(case_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS evidence_documents (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_key TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                fetched_at TEXT NOT NULL,
                UNIQUE(source, source_key)
            );
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
            (3, MIGRATION_VERSIONS[3]),
        )

"""History database for tracking deployments."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class DeploymentRecord:
    """A record of a deployment or rollback operation."""

    app: str
    binary_hash: str
    remote_path: str
    action: str  # 'push' or 'rollback'
    success: bool
    timestamp: Optional[str] = None
    git_hash: Optional[str] = None
    backup_path: Optional[str] = None
    error_message: Optional[str] = None
    id: Optional[int] = None


class History:
    """Manages deployment history in a SQLite database."""

    def __init__(self, db_path: Path):
        self._db_path = db_path

    def init_db(self) -> None:
        """Initialize the database schema."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deployments (
                id INTEGER PRIMARY KEY,
                app TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                git_hash TEXT,
                binary_hash TEXT NOT NULL,
                remote_path TEXT NOT NULL,
                backup_path TEXT,
                action TEXT NOT NULL,
                success INTEGER NOT NULL,
                error_message TEXT
            )
        """)
        conn.commit()
        conn.close()

    def record(self, record: DeploymentRecord) -> None:
        """Record a deployment operation.

        Args:
            record: The deployment record to save.
        """
        timestamp = record.timestamp or datetime.now().isoformat()

        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """
            INSERT INTO deployments
            (app, timestamp, git_hash, binary_hash, remote_path, backup_path, action, success, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.app,
                timestamp,
                record.git_hash,
                record.binary_hash,
                record.remote_path,
                record.backup_path,
                record.action,
                1 if record.success else 0,
                record.error_message,
            ),
        )
        conn.commit()
        conn.close()

    def get_history(self, app: str, limit: Optional[int] = None) -> list[DeploymentRecord]:
        """Get deployment history for an app.

        Args:
            app: The app name.
            limit: Maximum number of records to return.

        Returns:
            List of deployment records, newest first.
        """
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row

        query = """
            SELECT * FROM deployments
            WHERE app = ?
            ORDER BY timestamp DESC
        """
        if limit:
            query += f" LIMIT {limit}"

        cursor = conn.execute(query, (app,))
        rows = cursor.fetchall()
        conn.close()

        return [self._row_to_record(row) for row in rows]

    def get_all_history(self, limit: Optional[int] = None) -> list[DeploymentRecord]:
        """Get deployment history for all apps.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of deployment records, newest first.
        """
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row

        query = "SELECT * FROM deployments ORDER BY timestamp DESC"
        if limit:
            query += f" LIMIT {limit}"

        cursor = conn.execute(query)
        rows = cursor.fetchall()
        conn.close()

        return [self._row_to_record(row) for row in rows]

    def get_last_deployment(self, app: str) -> Optional[DeploymentRecord]:
        """Get the most recent deployment for an app.

        Args:
            app: The app name.

        Returns:
            The most recent deployment record, or None if none exist.
        """
        records = self.get_history(app, limit=1)
        return records[0] if records else None

    def _row_to_record(self, row: sqlite3.Row) -> DeploymentRecord:
        """Convert a database row to a DeploymentRecord."""
        return DeploymentRecord(
            id=row["id"],
            app=row["app"],
            timestamp=row["timestamp"],
            git_hash=row["git_hash"],
            binary_hash=row["binary_hash"],
            remote_path=row["remote_path"],
            backup_path=row["backup_path"],
            action=row["action"],
            success=bool(row["success"]),
            error_message=row["error_message"],
        )

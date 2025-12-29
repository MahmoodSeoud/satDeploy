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
    module: str = "default"
    timestamp: Optional[str] = None
    git_hash: Optional[str] = None
    backup_path: Optional[str] = None
    error_message: Optional[str] = None
    service_hash: Optional[str] = None
    vmem_cleared: bool = False
    id: Optional[int] = None


class History:
    """Manages deployment history in a SQLite database."""

    def __init__(self, db_path: Path):
        self._db_path = db_path

    def init_db(self) -> None:
        """Initialize the database schema."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self._db_path)

        # Check if table exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='deployments'"
        )
        table_exists = cursor.fetchone() is not None

        if table_exists:
            # Migrate existing table
            self._migrate(conn)
        else:
            # Create new table
            conn.execute("""
                CREATE TABLE deployments (
                    id INTEGER PRIMARY KEY,
                    module TEXT NOT NULL DEFAULT 'default',
                    app TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    git_hash TEXT,
                    binary_hash TEXT NOT NULL,
                    remote_path TEXT NOT NULL,
                    backup_path TEXT,
                    action TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    error_message TEXT,
                    service_hash TEXT,
                    vmem_cleared INTEGER NOT NULL DEFAULT 0
                )
            """)

        conn.commit()
        conn.close()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Migrate existing database to current schema."""
        cursor = conn.execute("PRAGMA table_info(deployments)")
        columns = {row[1] for row in cursor.fetchall()}

        # Add module column if missing
        if "module" not in columns:
            conn.execute(
                "ALTER TABLE deployments ADD COLUMN module TEXT NOT NULL DEFAULT 'default'"
            )

        # Add service_hash column if missing
        if "service_hash" not in columns:
            conn.execute("ALTER TABLE deployments ADD COLUMN service_hash TEXT")

        # Add vmem_cleared column if missing
        if "vmem_cleared" not in columns:
            conn.execute(
                "ALTER TABLE deployments ADD COLUMN vmem_cleared INTEGER NOT NULL DEFAULT 0"
            )

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
            (module, app, timestamp, git_hash, binary_hash, remote_path, backup_path, action, success, error_message, service_hash, vmem_cleared)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.module,
                record.app,
                timestamp,
                record.git_hash,
                record.binary_hash,
                record.remote_path,
                record.backup_path,
                record.action,
                1 if record.success else 0,
                record.error_message,
                record.service_hash,
                1 if record.vmem_cleared else 0,
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

    def get_module_state(self, module: str) -> dict[str, DeploymentRecord]:
        """Get last known state of all apps on a module.

        Args:
            module: The module name.

        Returns:
            Dict mapping app name to most recent DeploymentRecord for that app.
        """
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row

        # Get distinct apps for this module, then get the most recent for each
        query = """
            SELECT * FROM deployments d1
            WHERE module = ?
            AND timestamp = (
                SELECT MAX(timestamp) FROM deployments d2
                WHERE d2.module = d1.module AND d2.app = d1.app
            )
        """
        cursor = conn.execute(query, (module,))
        rows = cursor.fetchall()
        conn.close()

        return {row["app"]: self._row_to_record(row) for row in rows}

    def get_fleet_status(self) -> dict[str, dict[str, DeploymentRecord]]:
        """Get state of all modules.

        Returns:
            Dict mapping module name to dict of app name to DeploymentRecord.
        """
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row

        # Get all distinct modules
        cursor = conn.execute("SELECT DISTINCT module FROM deployments")
        modules = [row["module"] for row in cursor.fetchall()]
        conn.close()

        # Get state for each module
        return {module: self.get_module_state(module) for module in modules}

    def _row_to_record(self, row: sqlite3.Row) -> DeploymentRecord:
        """Convert a database row to a DeploymentRecord."""
        return DeploymentRecord(
            id=row["id"],
            module=row["module"],
            app=row["app"],
            timestamp=row["timestamp"],
            git_hash=row["git_hash"],
            binary_hash=row["binary_hash"],
            remote_path=row["remote_path"],
            backup_path=row["backup_path"],
            action=row["action"],
            success=bool(row["success"]),
            error_message=row["error_message"],
            service_hash=row["service_hash"],
            vmem_cleared=bool(row["vmem_cleared"]),
        )

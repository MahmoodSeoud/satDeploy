"""History database for tracking deployments."""

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class DeploymentRecord:
    """A record of a deployment or rollback operation."""

    app: str
    file_hash: str
    remote_path: str
    action: str  # 'push' or 'rollback'
    success: bool
    module: str = "default"
    timestamp: Optional[str] = None
    git_hash: Optional[str] = None
    provenance_source: Optional[str] = None  # "local", "ci/github", or "manual"
    backup_path: Optional[str] = None
    error_message: Optional[str] = None
    service_hash: Optional[str] = None
    vmem_cleared: bool = False
    transport: Optional[str] = None  # "ssh" or "csp"
    source: Optional[str] = None  # "cli" or "web" (which surface triggered the action)
    id: Optional[int] = None


# Status sentinels for ValidationRecord.status. Strings (not bools) because
# the spec is "PASS / FAIL record" and the design-doc thesis metric #3 reads
# the same way ("zero untested binaries reaching flight"). Keeping it as text
# also leaves room to add a third status (SKIPPED / TIMEOUT) without a schema
# migration.
VALIDATION_PASS = "PASS"
VALIDATION_FAIL = "FAIL"


@dataclass
class ValidationRecord:
    """A record of a `satdeploy validate` run.

    Keyed by (target, app, file_hash). Lookup `(target, app, file_hash) →
    PASS exists?` is the only thing the `--requires-validated` flight gate
    needs to know — but we also keep exit_code, duration, and
    stdout/stderr so post-mortems can ask "why did flatsat refuse this
    hash?" weeks after the fact.
    """

    target: str
    app: str
    file_hash: str
    status: str  # VALIDATION_PASS or VALIDATION_FAIL
    exit_code: int
    duration_ms: int
    command: str
    stdout: str = ""
    stderr: str = ""
    timestamp: Optional[str] = None
    id: Optional[int] = None


class History:
    """Manages deployment history in a SQLite database."""

    def __init__(self, db_path: Path):
        self._db_path = db_path

    def init_db(self) -> None:
        """Initialize the database schema."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self._db_path)

        # WAL mode + busy timeout for concurrent access with satdeploy-apm
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

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
                    file_hash TEXT NOT NULL,
                    remote_path TEXT NOT NULL,
                    backup_path TEXT,
                    action TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    error_message TEXT,
                    service_hash TEXT,
                    vmem_cleared INTEGER NOT NULL DEFAULT 0,
                    provenance_source TEXT,
                    transport TEXT,
                    source TEXT NOT NULL DEFAULT 'cli'
                )
            """)

        self._ensure_validations_table(conn)

        conn.commit()
        conn.close()

    def _ensure_validations_table(self, conn: sqlite3.Connection) -> None:
        """Create the validations table + lookup index if absent.

        Side table (not a column on `deployments`): a hash may be validated
        independently of any deploy and re-validated multiple times. Keyed by
        (target, app, file_hash) per the R1 fleet contract — a PASS on som1
        does NOT satisfy a `push --target flight --requires-validated`.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS validations (
                id INTEGER PRIMARY KEY,
                target TEXT NOT NULL,
                app TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_code INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                command TEXT NOT NULL,
                stdout TEXT,
                stderr TEXT,
                timestamp TEXT NOT NULL
            )
        """)
        # Lookup index for the gate: (target, app, file_hash, status='PASS').
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_validations_lookup
            ON validations(target, app, file_hash, status)
        """)

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

        # Add git_hash column if missing (for databases created before provenance)
        if "git_hash" not in columns:
            conn.execute("ALTER TABLE deployments ADD COLUMN git_hash TEXT")

        if "provenance_source" not in columns:
            conn.execute("ALTER TABLE deployments ADD COLUMN provenance_source TEXT")

        # Add transport column if missing (distinguishes SSH vs CSP deploys)
        if "transport" not in columns:
            conn.execute("ALTER TABLE deployments ADD COLUMN transport TEXT")

        # Add source column if missing (distinguishes CLI-triggered vs web-triggered actions).
        # Uses NOT NULL DEFAULT 'cli' so that existing rows inherit the historical CLI-only
        # assumption, while new writes from the dashboard can record "web" to support R6
        # audit-trail legibility + eng-review landmine #7 (no hardcoded "ssh"/"cli" constants).
        if "source" not in columns:
            conn.execute(
                "ALTER TABLE deployments ADD COLUMN source TEXT NOT NULL DEFAULT 'cli'"
            )

    def record(self, record: DeploymentRecord) -> None:
        """Record a deployment operation.

        Args:
            record: The deployment record to save.
        """
        timestamp = record.timestamp or datetime.now().isoformat()

        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            INSERT INTO deployments
            (module, app, timestamp, git_hash, file_hash, remote_path, backup_path,
             action, success, error_message, service_hash, vmem_cleared,
             provenance_source, transport, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.module,
                record.app,
                timestamp,
                record.git_hash,
                record.file_hash,
                record.remote_path,
                record.backup_path,
                record.action,
                1 if record.success else 0,
                record.error_message,
                record.service_hash,
                1 if record.vmem_cleared else 0,
                record.provenance_source,
                record.transport or "ssh",
                # Env-var fallback lets the dashboard shell out to the existing
                # CLI rollback path while tagging the audit row as web-initiated,
                # without threading an extra arg through every record() call site.
                record.source or os.environ.get("SATDEPLOY_SOURCE", "cli"),
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
        keys = row.keys()
        return DeploymentRecord(
            id=row["id"],
            module=row["module"],
            app=row["app"],
            timestamp=row["timestamp"],
            git_hash=row["git_hash"],
            file_hash=row["file_hash"],
            remote_path=row["remote_path"],
            backup_path=row["backup_path"],
            action=row["action"],
            success=bool(row["success"]),
            error_message=row["error_message"],
            service_hash=row["service_hash"],
            vmem_cleared=bool(row["vmem_cleared"]),
            provenance_source=row["provenance_source"] if "provenance_source" in keys else None,
            transport=row["transport"] if "transport" in keys else None,
            source=row["source"] if "source" in keys else None,
        )

    # ------------------------------------------------------------------
    # Validations (side table — see _ensure_validations_table)
    # ------------------------------------------------------------------

    def record_validation(self, record: ValidationRecord) -> None:
        """Persist a validation run.

        Multiple PASS rows for the same (target, app, file_hash) are allowed
        (re-validation after a fix, scheduled re-runs); the gate just checks
        whether *any* PASS row exists.
        """
        timestamp = record.timestamp or datetime.now().isoformat()

        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            INSERT INTO validations
            (target, app, file_hash, status, exit_code, duration_ms,
             command, stdout, stderr, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.target,
                record.app,
                record.file_hash,
                record.status,
                record.exit_code,
                record.duration_ms,
                record.command,
                record.stdout,
                record.stderr,
                timestamp,
            ),
        )
        conn.commit()
        conn.close()

    def has_pass_record(self, *, target: str, app: str, file_hash: str) -> bool:
        """Return True iff at least one PASS validation exists for this triple.

        This is the predicate the `push --requires-validated` flight gate
        consults. Keyed by all three of (target, app, file_hash) — a hash
        validated on som1 does NOT satisfy the gate on flight (R1 fleet
        contract: target_name is part of the validation key).
        """
        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute(
            """
            SELECT 1 FROM validations
            WHERE target = ? AND app = ? AND file_hash = ? AND status = ?
            LIMIT 1
            """,
            (target, app, file_hash, VALIDATION_PASS),
        )
        row = cursor.fetchone()
        conn.close()
        return row is not None

    def get_validation_history(
        self,
        *,
        app: str,
        target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[ValidationRecord]:
        """Return validation records for an app (optionally scoped to one target).

        Newest first. Used by `satdeploy validate` for human-readable output
        and (eventually) by the dashboard to surface validation provenance.
        """
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row

        if target is None:
            query = "SELECT * FROM validations WHERE app = ? ORDER BY timestamp DESC"
            params: tuple = (app,)
        else:
            query = (
                "SELECT * FROM validations WHERE app = ? AND target = ? "
                "ORDER BY timestamp DESC"
            )
            params = (app, target)

        if limit:
            query += f" LIMIT {limit}"

        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        return [
            ValidationRecord(
                id=row["id"],
                target=row["target"],
                app=row["app"],
                file_hash=row["file_hash"],
                status=row["status"],
                exit_code=row["exit_code"],
                duration_ms=row["duration_ms"],
                command=row["command"],
                stdout=row["stdout"] or "",
                stderr=row["stderr"] or "",
                timestamp=row["timestamp"],
            )
            for row in rows
        ]

# Week 3: Logs, History Database, and Error Handling

## Features to Implement

1. **`satdeploy logs <app>`** - Tail journalctl for a service
   - Uses existing `ServiceManager.get_logs()` method
   - Add CLI command with options for number of lines
   - Optional: follow mode (`-f`)

2. **History Database (SQLite)** - Track deployments in `~/.satdeploy/history.db`
   - Schema from PLAN.md:
     ```sql
     CREATE TABLE deployments (
       id INTEGER PRIMARY KEY,
       app TEXT NOT NULL,
       timestamp TEXT NOT NULL,
       git_hash TEXT,
       binary_hash TEXT NOT NULL,
       remote_path TEXT NOT NULL,
       backup_path TEXT,
       action TEXT NOT NULL,  -- 'push' | 'rollback'
       success INTEGER NOT NULL,
       error_message TEXT
     );
     ```
   - Log push and rollback operations
   - Query history for display

3. **Error Handling & Edge Cases**
   - SSH connection failures with retry logic
   - File not found errors
   - Permission errors
   - Service restart failures
   - Backup directory creation failures

## Implementation Notes

- `ServiceManager.get_logs()` already exists in services.py
- Need to create `satdeploy/history.py` module
- Integrate history logging into push/rollback commands

## Implementation Complete

All Week 3 features implemented:

1. **logs command** - Added `satdeploy logs <app>` with `--lines/-n` option
2. **history.py** - Created SQLite-based deployment history tracking
3. **Push logging** - Records successful/failed pushes with binary hash
4. **Rollback logging** - Records successful/failed rollbacks with backup path
5. **SSH error handling** - Graceful handling of connection errors:
   - Authentication failures
   - Connection refused
   - Connection timeout
   - Host key verification errors

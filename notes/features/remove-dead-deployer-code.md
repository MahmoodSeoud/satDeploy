# Remove Dead Deployer Code

## Objective
Remove unused code from `deployer.py` following John Carmack's principle: don't keep dead code around - it creates confusion and maintenance burden.

## Analysis

### Dead Code Identified
1. **`DeployResult` dataclass** (lines 37-46) - Never imported or used by cli.py
2. **`Deployer.push()` method** (lines 165-220) - CLI implements push logic inline
3. **`Deployer.rollback()` method** (lines 222-289) - CLI implements rollback logic inline

### Evidence
- `grep` shows cli.py does NOT import DeployResult
- `grep` shows cli.py does NOT call deployer.push() or deployer.rollback()
- CLI uses primitives directly: backup(), deploy(), compute_hash(), list_backups()

### Orphaned Tests
Tests that exercise the dead code (must also be removed):
- `TestPush` class (lines 201-370 in test_deployer.py)
- `TestRollback` class (lines 462-718 in test_deployer.py)

### Code to Keep
These are used by cli.py and must remain:
- `parse_backup_version()` function
- `Deployer.__init__()`
- `Deployer.compute_hash()`
- `Deployer.compute_remote_hash()`
- `Deployer.list_backups()`
- `Deployer.backup()`
- `Deployer.deploy()`

## Why CLI Reimplements Push/Rollback

The CLI has more complex requirements than the Deployer methods:
1. CLI handles **dependency chains** (stop/start multiple services in order)
2. CLI manages **step counting** for progress display
3. CLI records to **history database**
4. CLI handles **dial-style rollback** (stepping through versions)

The Deployer methods assume a simpler model (single service stop/start). The CLI's inline implementation is the correct, complete version.

## Refactoring Steps
1. Remove `DeployResult` dataclass from deployer.py
2. Remove `push()` method from deployer.py
3. Remove `rollback()` method from deployer.py
4. Remove unused import `from satdeploy.services import ServiceManager` TYPE_CHECKING
5. Remove `TestPush` class from test_deployer.py
6. Remove `TestRollback` class from test_deployer.py
7. Update test import to not import DeployResult

## Metrics
- deployer.py: 290 → 149 lines (removed 141 lines, -49%)
- test_deployer.py: 719 → 283 lines (removed 436 lines, -61%)
- Tests: 198 → 182 (removed 16 orphaned tests)
- All 182 remaining tests pass

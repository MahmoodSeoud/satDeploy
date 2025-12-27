# List Backup Bug Investigation

## Problem Statement
After a fresh start (no backups, no binaries), pushing an app twice should show two different versions in `list`. The old behavior only showed backups, not the currently deployed version.

## Root Cause
The `list` command was only showing backup files, not the currently deployed version. After 2 pushes:
- v1 is backed up
- v2 is currently deployed

But `list` only showed v1 (the backup), not v2 (the deployed version).

## Solution
Updated `list` command to show both:
1. Currently deployed version (from history) - marked as "deployed"
2. All backups - marked as "backup"

## Changes Made
- `cli.py`: Updated `list_backups()` function to:
  - Show "Versions for {app}:" instead of "Backups for {app}:"
  - Display currently deployed version first (from history)
  - Add STATUS column (deployed/backup)
  - Updated --help text
- `README.md`: Updated command description and example output
- `CLAUDE.md`: Updated CLI usage description
- Added tests in `tests/test_push_list_integration.py`

## Example Output After Fix
```
$ satdeploy list test_app
Versions for test_app:

    HASH       TIMESTAMP            STATUS
    ---------------------------------------------
  → 32eb164d  2025-12-27 00:55:53  deployed
  • fb496f1d  2025-12-27 00:55:52  backup
```

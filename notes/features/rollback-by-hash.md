# Feature: Rollback by Hash

## Summary
Allow `satdeploy rollback <app> <hash>` to rollback by specifying the 8-character hash prefix.

## Final Behavior
- `satdeploy rollback controller` - dial behavior, goes to next older version
- `satdeploy rollback controller def67890` - rollback to specific hash

## Implementation
- CLI argument renamed from `version` to `hash`
- Only matches by hash prefix (full version string no longer supported)
- Error message: "Hash {hash} not found"

## Test Cases
1. Rollback by valid hash prefix finds correct backup
2. Rollback by unknown hash fails with "not found" error

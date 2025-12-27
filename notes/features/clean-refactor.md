# Clean Refactor Notes

## Objective
Perform a clean refactor of the codebase following John Carmack's principles:
- Simplicity over complexity
- Direct, clear code paths
- Remove unnecessary abstractions
- Prefer local reasoning over global state

## Changes Made

### Refactoring
1. **Extracted `get_services_to_manage()` helper** - Removed 28 lines of duplicated
   code between push and rollback commands for dependency resolution
2. **Extracted `format_iso_timestamp()` helper** - Removed duplicate inline imports
   and try/except blocks for timestamp formatting
3. **Moved datetime import to module level** - Cleaner imports

### Bug Fix
4. **Fixed rollback command** - Two bugs were discovered and fixed:
   - Rollback now backs up the current version BEFORE restoring (like push does)
   - Rollback now skips backups matching the currently deployed version
   - This ensures versions are never lost and rollback cycles correctly

## Analysis of Current Code

### Identified Issues

1. **cli.py (615 lines)** - The main issue:
   - Massive code duplication between `push` and `rollback` commands
   - Both commands have nearly identical service management logic
   - Both commands have identical dependency resolution code
   - Step counting logic is duplicated
   - The `status` command has datetime parsing duplicated inline

2. **deployer.py** - Has unused code:
   - `DeployResult` dataclass is not used by cli.py
   - `Deployer.push()` and `Deployer.rollback()` methods exist but are not used
   - CLI reimplements the same logic in a different way
   - This is a sign of evolving design without cleanup

3. **output.py** - Minimal but could use `info` function (currently returns input unchanged)

### What's Good
- Core modules (config, dependencies, history, services, ssh) are clean and focused
- Test coverage is comprehensive (195 tests)
- Clear separation of concerns between modules
- The deployer module has good primitives (backup, deploy, compute_hash, list_backups)

## Refactoring Plan

### Phase 1: Remove dead code from deployer.py
- `DeployResult` is not used by CLI
- `push()` and `rollback()` methods are not used
- Keep the primitives that ARE used: backup, deploy, compute_hash, list_backups, compute_remote_hash

### Phase 2: Extract service management from cli.py
- Create helper function for building services_to_manage list
- This logic is duplicated in push and rollback

### Phase 3: Simplify cli.py
- Extract common patterns into focused helper functions
- Reduce duplication between push/rollback commands

## Carmack's Approach Applied
- Don't abstract until you have 3+ clear instances
- Keep data flow visible and local
- Functions should be readable top-to-bottom
- Prefer explicit over implicit

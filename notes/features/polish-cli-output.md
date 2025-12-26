# Polish CLI Output

## Goal
Improve CLI output formatting with colors, symbols, and better visual structure.

## Implemented Changes

### Output Module (satdeploy/output.py)
New module with formatting utilities:
- `SYMBOLS` dict: check (✓), cross (✗), arrow (→), bullet (•)
- `success()`: Green text with checkmark
- `error()`: Red text with cross
- `warning()`: Yellow text
- `info()`: Plain text
- `step()`: Step counter like [1/5] in cyan

### Command Updates

**push command:**
- Step counters [1/N] for each operation (backup, upload, stop/start services)
- Arrow symbol (→) for file uploads
- Checkmark for success messages
- Warning styling for failed health checks

**rollback command:**
- Step counters [1/N] for restore and service operations
- Checkmark for success messages
- Warning styling for failed health checks

**status command:**
- Checkmark (green) for running services
- Cross (red) for failed services
- Bullet (yellow) for stopped services
- Bullet (green) for deployed libraries

**list command:**
- Bold header "Backups for <app>:"
- Bullet points for each backup entry
- Styled version and timestamp

**init command:**
- Bold header
- Checkmark on successful config save

**logs command:**
- Bold header with app name and service

## Test Coverage
Added 22 new tests for polished output formatting across all commands.

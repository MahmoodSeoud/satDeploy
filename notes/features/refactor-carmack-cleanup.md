# Carmack-Style Refactoring Notes

## Changes Made

### 1. Removed Dead Code

**output.py:**
- Removed `error()` function - never used
- Removed `info()` function - never used

**history.py:**
- Removed unused `field` import from dataclasses

**cli.py:**
- Removed unused `error` and `info` imports

### 2. Eliminated Duplicated App Config Lookup

Replaced 4 instances of this pattern:
```python
app_config = config.get_app(app)
if app_config is None:
    raise click.ClickException(f"App '{app}' not found...")
```

With the existing helper:
```python
app_config = get_app_config_or_error(config, app)
```

### 3. Extracted Common Deployment Logic (cli.py)

Created helper functions to eliminate duplicated service management:

**StepCounter class:**
Simple counter that formats and prints step messages. Replaces the repetitive:
```python
current_step += 1
click.echo(step(current_step, total_steps, message))
```

**stop_services():**
Stops services in order with progress output. Extracted from push (2 places) and rollback.

**start_services():**
Starts services in reverse order with health checks. Extracted from push (2 places) and rollback.

**restore_backup():**
Copies backup file to remote path and makes it executable. Used by both push (restore from backup) and rollback.

### 4. Code Not Changed

**config.py properties:**
Initially considered refactoring the `if self._data is None` pattern, but decided against it. The current pattern is:
- Clear and explicit
- Each property handles None differently (different defaults)
- No real duplication - just 2 lines per property
- Adding abstraction would make it harder to understand

Carmack principle: don't add abstraction until you need it.

## Metrics

- Lines removed: ~60
- Lines added: ~40 (helper functions)
- Net reduction: ~20 lines
- Tests still passing: 181/181

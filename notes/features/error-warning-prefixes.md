# Error and Warning Prefixes Feature

## Goal
Add `[ERROR]` and `[WARNING]` prefixes to all error and warning messages in the CLI output.

## Current State
- `output.py` has `error()` and `warning()` functions that format messages with colors
- `error()` returns red-colored text
- `warning()` returns yellow-colored text
- Neither currently includes a prefix

## Implementation Plan
1. Modify `error()` to prepend `[ERROR]` to all messages
2. Modify `warning()` to prepend `[WARNING]` to all messages
3. Ensure `SatDeployError` also gets the prefix via `error()`

## Files to Modify
- `satdeploy/output.py` - Core formatting functions
- `tests/test_output.py` - Tests for the output module

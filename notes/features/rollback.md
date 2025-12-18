# Phase 3: Rollback Feature

## Overview
Implement rollback functionality to restore previous binary versions when a deployment fails or needs to be reverted.

## Requirements (from plan.md)

### Agent: rollback command
- Copy `.prev` file back to binary path
- Restart services (with dependency ordering)
- Return JSON response

### CLI: rollback command
- SSH to agent and run rollback
- Display formatted output

### Edge Cases
- No previous version for rollback: Return error with clear message

## Implementation Plan

### sat-agent rollback
1. Validate service exists
2. Check backup file exists (`<backup_dir>/<service>.prev`)
3. Stop services in dependency order (same as deploy)
4. Copy `.prev` back to binary path
5. Make executable (chmod +x)
6. Start services in reverse order
7. Verify service is running
8. Return JSON result with hash of restored binary

### sat CLI rollback
1. Validate service exists in config
2. SSH to agent: `sat-agent rollback <service>`
3. Parse JSON response
4. Display success/failure with nice formatting

## Test Cases

### Agent Tests
- `test_rollback_returns_success_json` - Basic success case
- `test_rollback_copies_prev_to_binary` - Verifies file operations
- `test_rollback_makes_executable` - chmod +x
- `test_rollback_stops_services_in_order` - Dependency ordering
- `test_rollback_starts_services_in_order` - Reverse dependency ordering
- `test_rollback_fails_if_no_backup` - Edge case: no .prev file
- `test_rollback_fails_for_unknown_service` - Invalid service name
- `test_rollback_fails_if_service_not_running_after` - Service fails to start

### CLI Tests
- `test_rollback_calls_agent_via_ssh` - Correct SSH command
- `test_rollback_returns_0_on_success` - Success exit code
- `test_rollback_returns_1_on_ssh_failure` - SSH error handling
- `test_rollback_returns_1_on_agent_error` - Agent error handling
- `test_rollback_returns_1_for_unknown_service` - Validation

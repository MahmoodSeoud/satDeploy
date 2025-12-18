# Phase 4: Polish

## Scope
- Timing output ("Deployed in 34s")
- CLI `logs` command (SSH + journalctl -f)
- CLI `restart` command
- Agent `restart` command
- Better error messages
- install-agent.sh script

## Implementation Order
1. Timing output for deploy command
2. Logs command (simplest - just SSH wrapper)
3. Restart command (agent + CLI)
4. Better error messages
5. Install script

## Completed

All items implemented:
- [x] Timing output with human-readable format (0.5s, 1m 30s)
- [x] Logs command streams journalctl via SSH
- [x] Restart command in both agent and CLI
- [x] Config error messages include hints about env vars
- [x] install-agent.sh script

## Test Summary
- 80 total tests (up from 62)
- New tests: timing output (3), logs (3), restart CLI (5), restart agent (4), main CLI (2)

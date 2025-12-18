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

## Notes
- Logs command streams output, doesn't capture
- Restart is similar to deploy but without binary swap
- Timing should use time.time() and format human-readable

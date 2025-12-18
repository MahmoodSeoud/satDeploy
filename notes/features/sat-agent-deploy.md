# sat-agent-deploy Feature Notes

## Overview
Implementing the `deploy` command for sat-agent which handles atomic binary deployment with dependency-aware service restarts.

## Deploy Command Workflow
1. Receive: `sat-agent deploy <service_name>`
2. Binary already uploaded as `<path>.new` by CLI
3. Stop services (top-down: service + all dependents)
4. Backup current binary to `/opt/sat-agent/backups/<service>.prev`
5. Move `.new` to final path
6. chmod +x
7. Start services (bottom-up: dependencies first)
8. Log to `/opt/sat-agent/versions.json`

## Dependency Resolution
Given config:
```
controller depends_on: [csp_server]
csp_server depends_on: [param_handler]
param_handler depends_on: []
```

If deploying `csp_server`:
- Stop order (top-down): controller, csp_server
- Start order (bottom-up): csp_server, controller

## Key Functions to Implement
- `get_dependents(service, config)` - find all services that depend on this one
- `get_stop_order(service, config)` - return services to stop (top-down)
- `get_start_order(service, config)` - return services to start (bottom-up)
- `stop_service(service)` - systemctl stop
- `start_service(service)` - systemctl start
- `backup_binary(service, config)` - copy current to .prev
- `swap_binary(service, config)` - move .new to final, chmod +x
- `log_deployment(service, config)` - append to versions.json
- `deploy(service, config)` - orchestrate full deployment

## Output Format
Success:
```json
{"status": "ok", "service": "csp_server", "hash": "a3f2c9b1"}
```

Failure:
```json
{"status": "failed", "reason": "service not running after start"}
```

## Edge Cases
- First deploy (no existing binary): skip backup
- .new file doesn't exist: error
- Service won't start: error, don't mark success

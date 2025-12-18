# Project: sat-deploy

## Overview

Build a CLI tool for fast binary deployment to an embedded Linux flatsat (satellite engineering model). The goal is to reduce deployment time from ~10 minutes (manual) to ~30 seconds (automated).

## Architecture

```
┌─────────────────┐         SSH + rsync         ┌─────────────────┐
│   Developer     │ ──────────────────────────► │    Flatsat      │
│   Laptop        │                             │   (Yocto Linux) │
│                 │                             │                 │
│  sat (CLI)      │────── commands over SSH ───►│  sat-agent      │
│                 │◄───── JSON responses ───────│                 │
└─────────────────┘                             └─────────────────┘
```

## Components

### 1. sat-agent (runs on flatsat)

- **Location:** `/opt/sat-agent/sat-agent`
- **Language:** Python 3
- **Purpose:** Receives commands, executes deployments, manages services
- **Invoked via:** SSH from the CLI

**Commands it must handle:**

```bash
sat-agent deploy <service_name>    # Deploy a .new binary, restart services
sat-agent rollback <service_name>  # Restore previous binary
sat-agent status                   # Return status of all services
sat-agent restart <service_name>   # Restart service + dependents
```

**All commands return JSON to stdout:**
```json
{"status": "ok", "service": "controller"}
{"status": "failed", "reason": "service not running after start"}
```

**Key behaviors:**

1. **Dependency-aware restarts:**
   - Read dependency graph from config
   - When deploying service X, stop X and everything that depends on X (top-down)
   - After deploying, start in reverse order (bottom-up)

2. **Atomic deployment:**
   - Binary arrives as `<path>.new`
   - Backup current to `/opt/sat-agent/backups/<service>.prev`
   - Move `.new` to final path
   - chmod +x

3. **Version logging:**
   - Log each deployment to `/opt/sat-agent/versions.json`
   - Include: service name, sha256 hash (first 8 chars), timestamp

4. **Rollback:**
   - Copy `.prev` back to binary path
   - Restart services

5. **Service management via systemd:**
   - `systemctl stop/start/is-active <service>.service`

### 2. sat (CLI, runs on developer laptop)

- **Location:** Project directory, invoked as `./sat` or added to PATH
- **Language:** Python 3
- **Dependencies:** PyYAML (`pip install pyyaml`)

**Commands:**

```bash
sat deploy <service> <local_binary>   # Upload + deploy
sat rollback <service>                # Rollback to previous
sat status                            # Show all service statuses  
sat restart <service>                 # Restart service + dependents
sat logs <service>                    # Tail logs (journalctl -f)
```

**Workflow for `sat deploy controller ./build/controller`:**

1. Load `config.yaml`
2. rsync `./build/controller` to `flatsat:/opt/disco/bin/controller.new`
3. SSH and run: `sat-agent deploy controller`
4. Parse JSON response
5. Print success/failure with nice formatting

### 3. config.yaml

```yaml
flatsat:
  host: flatsat-disco.local    # Hostname or IP
  user: root                   # SSH user

backup_dir: /opt/sat-agent/backups
version_log: /opt/sat-agent/versions.json

services:
  controller:
    binary: /opt/disco/bin/controller
    systemd: controller.service
    depends_on:
      - csp_server

  csp_server:
    binary: /opt/disco/bin/csp_server
    systemd: csp_server.service
    depends_on:
      - param_handler

  param_handler:
    binary: /opt/disco/bin/param_handler
    systemd: param_handler.service
    depends_on: []
```

**Note:** `depends_on` means "this service requires these to be running first". So if deploying `csp_server`, we must also restart `controller` (which depends on it).

## File Structure

```
sat-deploy/
├── sat                     # CLI script (Python, executable)
├── sat-agent               # Agent script (Python, executable)
├── config.yaml             # Configuration
├── install-agent.sh        # Script to install agent on flatsat
└── README.md
```

## Implementation Order

### Phase 1: Agent core (Day 1-2)

Create `sat-agent` with:
- [ ] Load config from `/opt/sat-agent/config.yaml`
- [ ] `status` command - return running/stopped for each service
- [ ] `deploy` command - stop services, swap binary, start services
- [ ] JSON output for all commands
- [ ] Error handling - catch exceptions, return JSON errors

Test by SSHing to flatsat and running manually.

### Phase 2: CLI core (Day 2-3)

Create `sat` with:
- [ ] Load local `config.yaml`
- [ ] `status` command - SSH to agent, print formatted output
- [ ] `deploy` command - rsync + SSH to agent
- [ ] Nice terminal output with checkmarks/X marks

### Phase 3: Rollback (Day 3-4)

- [ ] Agent: backup binary before deploying
- [ ] Agent: `rollback` command
- [ ] CLI: `rollback` command

### Phase 4: Polish (Day 4-5)

- [ ] CLI: `logs` command (just SSH + journalctl -f)
- [ ] CLI: `restart` command
- [ ] Better error messages
- [ ] Timing output ("Deployed in 34 seconds")
- [ ] install-agent.sh script

## Code Style

- Python 3.8+
- Use subprocess.run() with check=True for commands that must succeed
- Use pathlib.Path for file operations
- Keep it simple - no frameworks, no async, no classes unless needed
- Each file should be under 150 lines

## Testing

Manual testing workflow:

```bash
# Terminal 1: Watch flatsat logs
ssh flatsat journalctl -f

# Terminal 2: Run commands
./sat status
./sat deploy controller ./build/controller
./sat rollback controller
```

## Example Session

```
$ ./sat status
[✓] controller: running
[✓] csp_server: running  
[✓] param_handler: running

$ ./sat deploy controller ./build/controller
[~] Uploading controller...
[~] Stopping: controller
[~] Deploying controller (a3f2c9b)
[~] Starting: controller
[✓] Deployed controller in 28s

$ ./sat rollback controller
[~] Rolling back controller...
[✓] Rolled back to previous version

$ ./sat logs controller
-- Logs begin at ... --
<streaming logs>
```

## Edge Cases to Handle

1. **Binary doesn't exist yet (first deploy):** Skip backup, just deploy
2. **Service won't start after deploy:** Return error, don't mark as success
3. **No previous version for rollback:** Return error with clear message
4. **SSH connection fails:** Catch and show helpful error
5. **rsync fails:** Catch and show helpful error

## Future Improvements (NOT in MVP)

- Health checks (configurable command to verify service is healthy)
- Git commit hash in version log
- Multi-target (multiple flatsats)
- CI/CD integration
- Web UI
- Automatic rollback on health check failure

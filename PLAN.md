# sat-deploy v0.1

## The Problem

Deploying binaries to an embedded Linux target during development is manual and error-prone. You're either using a janky uploader, a USB stick, or SSH + prayer. No versioning, no rollback, no dependency awareness.

## The Solution

A CLI tool that deploys binaries to their real paths, keeps versioned backups, restarts services in dependency order, and lets you rollback in one command.

---

## CLI Interface

```
satdeploy init                     # Interactive setup, creates config.yaml
satdeploy push <app>               # Deploy binary, backup old, restart services
satdeploy push <app> --local ./x   # Override local path
satdeploy list <app>               # Show deployed versions with timestamps
satdeploy rollback <app>           # Restore previous version
satdeploy rollback <app> <version> # Restore specific version
satdeploy status                   # What's running? Service health?
satdeploy logs <app>               # Tail journalctl for the service
```

---

## How It Works

### `satdeploy push controller`

1. Read config, find `controller` entry
2. Compute sha256 hash of local binary (first 8 chars)
3. SSH to target
4. Backup current remote binary:
   ```
   /opt/disco/bin/controller → /opt/satdeploy/backups/controller/20240115-143022-a3f2c9b.bak
   ```
5. Copy local binary to remote path:
   ```
   ./build/controller → /opt/disco/bin/controller
   ```
6. chmod +x
7. Stop services top-down (dependents first, then the service itself)
8. Start services bottom-up (the service first, then dependents)
9. Health check: `systemctl is-active <service>`
10. Log deployment to local `history.db`
11. Print result

### `satdeploy rollback controller`

1. Find most recent backup in `/opt/satdeploy/backups/controller/`
2. Copy backup to remote path:
   ```
   /opt/satdeploy/backups/controller/20240115-143022-a3f2c9b.bak → /opt/disco/bin/controller
   ```
3. Restart services (same stop/start order as push)
4. Health check
5. Log rollback to `history.db`

### `satdeploy push libparam`

Libraries don't have their own service, but other services depend on them:

1. Backup `/usr/lib/libparam.so`
2. Copy new `.so` file
3. Restart all services in `restart` list (e.g., `csp_server`, `controller`)
4. Health check each
5. Log

---

## Config File

```yaml
# ~/.satdeploy/config.yaml

target:
  host: 192.168.1.50
  user: root
  # auth: key (default) | password

backup_dir: /opt/satdeploy/backups
max_backups: 10  # Per app, oldest deleted when exceeded

apps:
  csp_server:
    local: ./build/csp_server
    remote: /usr/bin/csp_server
    service: csp_server.service

  libparam:
    local: ./build/libparam.so
    remote: /usr/lib/libparam.so
    service: null
    restart: [csp_server, controller]  # Restart these when libparam changes

  controller:
    local: ./build/controller
    remote: /opt/disco/bin/controller
    service: controller.service
    depends_on: [csp_server]
```

---

## Local State

```
~/.satdeploy/
  config.yaml          # Target + app definitions
  history.db           # SQLite database
```

### history.db schema

```sql
CREATE TABLE deployments (
  id INTEGER PRIMARY KEY,
  app TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  git_hash TEXT,
  binary_hash TEXT NOT NULL,
  remote_path TEXT NOT NULL,
  backup_path TEXT,
  action TEXT NOT NULL,  -- 'push' | 'rollback'
  success INTEGER NOT NULL,
  error_message TEXT
);
```

---

## Dependency Resolution

Given this config:
```yaml
controller:
  depends_on: [csp_server]

csp_server:
  depends_on: [param_handler]

param_handler:
  depends_on: []
```

When deploying `param_handler`:

**Stop order (top-down):**
1. controller (depends on csp_server which depends on param_handler)
2. csp_server (depends on param_handler)
3. param_handler

**Start order (bottom-up):**
1. param_handler
2. csp_server
3. controller

The tool builds the dependency graph and computes the correct order.

---

## Tech Stack

- **Python 3.8+**
- **Paramiko** — SSH connection
- **Click** — CLI framework
- **SQLite** — Local history
- **PyYAML** — Config parsing

No external dependencies on the target. Just SSH access and systemd.

---

## File Structure

```
sat-deploy/
├── satdeploy/
│   ├── __init__.py
│   ├── cli.py           # Click commands
│   ├── config.py        # Load/validate config.yaml
│   ├── deployer.py      # Push/rollback logic
│   ├── ssh.py           # SSH connection wrapper
│   ├── services.py      # Systemd start/stop/health
│   ├── dependencies.py  # Dependency graph resolution
│   └── history.py       # SQLite operations
├── config.example.yaml
├── setup.py
└── README.md
```

---

## What This Is NOT (Yet)

- No web UI
- No multi-target fleet management
- No CI/CD integration
- No cloud anything
- No agent on target (just SSH)

---

## Future (v0.2+)

- **Multi-target:** Deploy to multiple devices in parallel
- **CI integration:** GitHub Action / GitLab CI
- **Health checks:** Custom commands, not just systemctl is-active
- **Hooks:** Pre-deploy and post-deploy scripts
- **Diffing:** Show what changed between versions

---

## Example Session

```
$ satdeploy status
Target: flatsat (192.168.1.50)

  controller      ✓ running    v20240115-143022-a3f2c9b
  csp_server      ✓ running    v20240115-091544-b7e1d2a
  param_handler   ✓ running    v20240112-162301-c4d5e6f
  libparam        ✓ deployed   v20240110-120000-1a2b3c4

$ satdeploy push controller
[1/5] Backing up /opt/disco/bin/controller
[2/5] Copying ./build/controller → /opt/disco/bin/controller
[3/5] Stopping controller.service
[4/5] Starting controller.service
[5/5] Health check passed

✓ Deployed controller (a3f2c9b → e5f6a7b) in 4.2s

$ satdeploy list controller
VERSION                     TIMESTAMP            HASH
20240115-143022-a3f2c9b     2024-01-15 14:30     a3f2c9b
20240114-091500-b2c3d4e     2024-01-14 09:15     b2c3d4e
20240113-160000-f1e2d3c     2024-01-13 16:00     f1e2d3c

$ satdeploy rollback controller
[1/4] Restoring 20240115-143022-a3f2c9b
[2/4] Stopping controller.service
[3/4] Starting controller.service
[4/4] Health check passed

✓ Rolled back controller to a3f2c9b in 2.8s
```

---

## Build Order

### Week 1
- [x] `satdeploy init` — Interactive config creation
- [x] `satdeploy push <app>` — Basic deploy (backup, copy, restart)
- [x] `satdeploy status` — Show service states

### Week 2
- [x] `satdeploy rollback <app>` — Restore from backup
- [x] `satdeploy list <app>` — Show version history
- [x] Dependency resolution (stop/start order)

### Week 3
- [x] `satdeploy logs <app>` — Tail journalctl
- [x] History database (SQLite)
- [x] Error handling, edge cases

### Week 4
- [x] Polish CLI output
- [x] README, documentation
- [x] Test on real flatsat


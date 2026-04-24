# Command Reference

The Python CLI handles SSH deployments. The CSH APM handles CSP deployments. Both provide the same five commands (`push`, `status`, `list`, `rollback`, `logs`) and write to the same history database.

> Every target-aware command accepts `-t/--target NAME` and reads `SATDEPLOY_TARGET` from the environment when the flag is absent. See [Multi-target](#multi-target-fleet) below for the fleet config shape.

## push: Deploy files to target

```
satdeploy push <app>                         # Deploy app from config
satdeploy push <app1> <app2>                 # Deploy multiple apps
satdeploy push -a / --all                    # Deploy all apps from config
satdeploy push -f PATH -r PATH               # Ad-hoc deploy (no config entry needed)
```

| Flag | Description |
|------|-------------|
| `-f, --local PATH` | Local file path (overrides config) |
| `-r, --remote PATH` | Remote path on target |
| `-F, --force` | Force deploy even if same version |
| `-a, --all` | Deploy all apps from config |
| `--require-clean` | Refuse to deploy from a dirty git tree |

## status: Show deployed apps

```
satdeploy status
```

Hashes the remote file and compares against the history database. Shows the git commit each live file was built from.

## list: Show version history

```
satdeploy list <app>
```

## rollback: Restore a previous version

```
satdeploy rollback <app>                     # Roll back to previous version
satdeploy rollback <app> -H HASH             # Roll back to specific version
```

| Flag | Description |
|------|-------------|
| `-H, --hash HASH` | Specific backup hash to restore |

## logs: View service logs

```
satdeploy logs <app>
satdeploy logs <app> -l 50                   # Show last 50 lines
```

| Flag | Description |
|------|-------------|
| `-l, --lines NUM` | Number of lines to show (default: 100) |

## config: Show current configuration

```
satdeploy config
```

## demo: Zero-prerequisite workflow demo

```
satdeploy demo           # Set up throwaway git repo + local target dir
satdeploy demo stop      # Tear down
satdeploy demo status    # Check if the demo is set up
```

Python CLI only (not available via the CSH APM).

## Shell completion

The easy way. Writes to the system completions directory (same place as `gh`, `docker`, `brew`), no rc file edit needed:

```bash
satdeploy completion --install
```

Or add it to your shell rc manually:

```bash
# Bash: add to ~/.bashrc
eval "$(_SATDEPLOY_COMPLETE=bash_source satdeploy)"

# Zsh: add to ~/.zshrc
eval "$(_SATDEPLOY_COMPLETE=zsh_source satdeploy)"
```

## Multi-target (fleet)

A single config can hold multiple targets. Target-aware commands (`push`, `iterate`, `watch`, `status`, `list`, `rollback`, `logs`, `config`) accept `-t/--target NAME` to pick one; omitting it uses `default_target` (or the first `targets:` entry). Shell completion knows the names.

```yaml
# ~/.satdeploy/config.yaml
default_target: som1

targets:
  som1:
    transport: ssh
    host: 192.168.1.50
    user: root
  som2:
    transport: ssh
    host: 192.168.1.51
    user: root
  flight:
    transport: csp
    zmq_endpoint: tcp://localhost:9600
    agent_node: 5425

apps:
  controller:
    local: ./build/controller
    remote: /opt/bin/controller
    service: controller.service
```

```bash
satdeploy push controller --target som2
satdeploy iterate controller -t som1
SATDEPLOY_TARGET=som1 satdeploy status
```

Per-target `backup_dir` is resolved via `Config.get_backup_dir(target_name)`; defaults are sensible for `local`, `ssh`, and `csp` transports. The history database (and the `satdeploy dev dashboard`) key rows by target so deploys never leak across modules.

Single-target (flat) configs still work unchanged — the loader lifts them into a one-entry `targets` dict internally.

## Global flags

All commands accept:

| Flag | Description |
|------|-------------|
| `-t, --target NAME` | Target name (reads `SATDEPLOY_TARGET` env var when absent) |
| `-n, --node NUM` | Override the target's CSP node (`agent_node`) |
| `--config PATH` | Config file (default: `~/.satdeploy/config.yaml`; reads `SATDEPLOY_CONFIG` env var) |

# Command Reference

The Python CLI and CSH APM share the same command interface. Every flag works in both.

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

## Global flags

All commands accept:

| Flag | Description |
|------|-------------|
| `-n, --node NUM` | Target CSP node (overrides `agent_node` from config) |
| `--config PATH` | Config file (default: `~/.satdeploy/config.yaml`) |

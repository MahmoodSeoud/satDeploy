# Command Reference

All commands run from inside CSH after `apm load`. They write to the shared `~/.satdeploy/history.db` so deploy history is unified across sessions.

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
| `-n, --node NUM` | Override the target's CSP node |

The first push for a new `(app, hash)` ships the whole file. If it doesn't
finish in one pass, the receive bitmap is persisted to
`/var/lib/satdeploy/state/<app>.dtpstate` on the target. The next push for
the same `(app, hash)` resumes from there — only the still-missing seqs go
on the wire.

## status: Show deployed apps

```
satdeploy status
```

Asks the agent to hash the live remote files and returns them along with
the git provenance recorded in history.db.

## list: Show version history

```
satdeploy list <app>
```

## rollback: Restore a previous version

```
satdeploy rollback <app>                     # Roll back to previous version
satdeploy rollback <app> <hash>              # Roll back to specific version
```

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

## Global flags

All commands accept:

| Flag | Description |
|------|-------------|
| `-n, --node NUM` | Override the target's CSP node (`agent_node`) |

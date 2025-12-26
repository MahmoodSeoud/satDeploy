# Week 1: init, push, status

## Features
- `satdeploy init` - Interactive config creation
- `satdeploy push <app>` - Basic deploy (backup, copy, restart)
- `satdeploy status` - Show service states

## Key Decisions
- Using Click for CLI framework (per PLAN.md)
- Using Paramiko for SSH
- Config stored at ~/.satdeploy/config.yaml
- PyYAML for config parsing

## Architecture Notes
- Config module handles loading/validating config.yaml
- SSH module wraps Paramiko for remote operations
- Services module handles systemd start/stop/health
- Deployer module contains push logic

## Progress
- [x] Project setup (pyproject.toml, package skeleton)
- [x] init command (interactive config creation)
- [x] push command (backup, copy, restart, health check)
- [x] status command (show service states)

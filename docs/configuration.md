# Configuration Reference

Each target gets its own config directory (e.g. `~/.satdeploy/som1/config.yaml`). Switch between targets with `--config`.

## Full example

```yaml
name: som1
transport: csp
zmq_endpoint: tcp://localhost:9600
agent_node: 5425
ground_node: 40
appsys_node: 10

backup_dir: /opt/satdeploy/backups
max_backups: 10

apps:
  controller:
    local: ./build/controller
    remote: /opt/disco/bin/controller
    service: controller.service
    depends_on: [csp_server]

  csp_server:
    local: ./build/csp_server
    remote: /usr/bin/csp_server
    service: csp_server.service

  libparam:
    local: ./build/libparam.so
    remote: /usr/lib/libparam.so
    service: null
    restart: [csp_server, controller]
```

## App options

| Field | Description |
|-------|-------------|
| `local` | Path to local file |
| `remote` | Deployment path on target |
| `service` | systemd service (null for libraries) |
| `depends_on` | Services this app depends on |
| `restart` | Services to restart when this library changes |
| `param` | libparam name for CSP start/stop |

## Transports

### SSH

Direct SSH/SFTP connection. Works with any Linux target.

```yaml
name: flatsat
transport: ssh
host: 192.168.1.50
user: root
```

### CSP

[CubeSat Space Protocol](https://github.com/spaceinventor/libcsp) over ZMQ, CAN, or KISS serial. Requires `satdeploy-agent` on the target.

```yaml
name: satellite
transport: csp
zmq_endpoint: tcp://localhost:9600
agent_node: 5425
ground_node: 40
```

## Dependency resolution

When deploying an app with dependencies:

1. **Stop** services top-down (dependents first)
2. **Deploy** the file
3. **Start** services bottom-up (dependencies first)

For libraries with a `restart` list, those services are restarted directly instead of computing the dependency graph.

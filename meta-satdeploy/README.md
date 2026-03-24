# meta-satdeploy

Yocto/OpenEmbedded layer for [satdeploy](https://github.com/MahmoodSeoud/satBuild) — OTA deployment for embedded Linux satellites.

## What this provides

- `satdeploy-agent` — the on-target agent that handles deploy, rollback, and status commands over CSP (CubeSat Space Protocol)

## Usage

1. Add this layer to your Yocto build:

    ```bash
    bitbake-layers add-layer /path/to/meta-satdeploy
    ```

2. Add to your image (in `local.conf` or your image recipe):

    ```
    IMAGE_INSTALL:append = " satdeploy-agent"
    ```

3. Build:

    ```bash
    bitbake your-image
    ```

The agent binary is built with your target's exact toolchain and linked against your target's libraries. No ABI surprises.

## Dependencies

This layer depends on:

- `openembedded-core` (or `poky`)
- `meta-oe` (for `libzmq`, `protobuf-c`, `libbsd`)
- `meta-networking` (for `libsocketcan`, if using CAN transport)

## Supported Yocto releases

- Kirkstone (LTS)
- Scarthgap (LTS)

## Pinning versions

The recipe defaults to `AUTOREV` (latest `main` branch). To pin to a specific release:

```
# In local.conf or your distro config:
SRCREV:pn-satdeploy-agent = "your-commit-hash"
```

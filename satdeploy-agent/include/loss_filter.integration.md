# Wiring the loss filter into the agent

This is a patch-style guide showing exactly what changes to make in the agent build + source to enable the `loss_filter` test scaffolding. Apply when you're ready to start running the trace-driven F3/F4/F5 experiments.

The filter is **compile-time gated** — the entire body of `loss_filter.c` and the per-call-site checks live behind `#ifdef SATDEPLOY_TEST_LOSS_FILTER`. Without that macro, the compiler emits zero filter code; flight builds are unaffected.

## 1. `meson.build` change

Add a meson option, conditional source, and conditional compile flag.

```meson
# satdeploy-agent/meson.build

# Add to default_options or as a separate option (cleaner):
option_test_loss_filter = get_option('test_loss_filter')

sources = files(
    'src/main.c',
    'src/deploy_handler.c',
    'src/backup_manager.c',
    'src/app_metadata.c',
    'src/dtp_client.c',
    'src/session_state.c',
    'src/sha256.c',
    'src/dtp_log_quiet.c',
    'proto/deploy.pb-c.c',
)

if option_test_loss_filter
    sources += files('src/loss_filter.c')
endif

# ... rest unchanged ...

c_args_extra = []
if option_test_loss_filter
    c_args_extra += '-DSATDEPLOY_TEST_LOSS_FILTER'
endif

satdeploy_agent = executable(
    ...
    c_args: [
        '-Os', '-Wall', '-Wextra',
        '-ffunction-sections', '-fdata-sections',
        '-fvisibility=hidden',
        '-fno-asynchronous-unwind-tables', '-fno-unwind-tables',
    ] + c_args_extra,
    ...
)
```

And add a `meson_options.txt`:

```meson
# satdeploy-agent/meson_options.txt
option('test_loss_filter', type: 'boolean', value: false,
       description: 'Compile in the trace-driven CSP packet drop hook for thesis experiments. NEVER enable for flight builds.')
```

Build the test variant:

```bash
cd satdeploy-agent
meson setup build-native --wipe -Dtest_loss_filter=true
ninja -C build-native
```

## 2. `main.c` — initialize and tear down

Add near the top of main(), BEFORE `csp_init()`:

```c
#include "loss_filter.h"

// ... in main() ...
if (loss_filter_init() != 0) {
    fprintf(stderr, "loss_filter: pattern file failed to load — refusing to start\n");
    return 1;
}
atexit(loss_filter_close);
```

The filter's wall clock starts ticking from `loss_filter_init()`, so this should be one of the first things main does. Refusing to start on a broken pattern file is intentional — silent fallback would invalidate the experiment.

## 3. `deploy_handler.c` — drop control packets

```diff
+#include "loss_filter.h"
+
 static void handle_connection(csp_conn_t *conn) {
     csp_packet_t *packet = csp_read(conn, 10000);
+    if (packet && loss_filter_should_drop()) {
+        csp_buffer_free(packet);
+        packet = NULL;
+    }
     if (packet == NULL) {
         printf("[deploy] error: no data received\n");
         fflush(stdout);
         return;
     }
```

This drops control-channel packets (port 20). At low loss rates this is rare; at high loss rates the deploy command itself can fail, which is realistic — the F3 chart should show this effect.

## 4. `dtp_client.c` — drop data packets

DTP runs its own internal loop, so hooking is more invasive. Two approaches; pick based on what libdtp exposes:

### Approach A — libdtp on_data hook (preferred)

`dtp_client.c` already calls `dtp_session_set_user_ctx`. If libdtp exposes an "on packet received" callback that can return "drop me," wire it:

```c
#include "loss_filter.h"

// In your on_data callback, before processing:
static int on_data_received(dtp_session_t *session, csp_packet_t *packet) {
    if (loss_filter_should_drop()) {
        csp_buffer_free(packet);
        return DTP_DROP;  // or whatever libdtp's "discard this packet" return is
    }
    // ... normal processing ...
}
```

Check libdtp's API — if `dtp_session_set_on_packet_recv` exists or similar, that's the hook.

### Approach B — wrap the CSP interface (fallback)

If libdtp doesn't have a usable RX hook, drop at the CSP interface layer. This requires implementing a "lossy wrapper" iface that forwards to a real iface but drops per the filter:

```c
// satdeploy-agent/src/lossy_iface.c
csp_iface_t lossy_iface;
static csp_iface_t *underlying;

static int lossy_nexthop(csp_iface_t *iface, uint16_t via,
                         csp_packet_t *packet, int from_me) {
    // For *outgoing* (TX), pass through.
    if (from_me) {
        return underlying->nexthop(underlying, via, packet, from_me);
    }
    // For *incoming* — actually nexthop is TX. RX drop needs a different hook.
    // ...
}
```

The clean RX-drop point in libcsp is harder. If approach A works, prefer it strongly.

## 5. Verify the wiring

A 60-second smoke test once you've built `-Dtest_loss_filter=true`:

```bash
# Generate a test pattern that drops 100% of packets after t=2s
cat > /tmp/test_pattern.pattern <<EOF
0.000 up
2.000 down
EOF

# Run the agent with the filter on
LOSS_PATTERN_FILE=/tmp/test_pattern.pattern \
    ./satdeploy-agent/build-native/satdeploy-agent -i ZMQ -p localhost -a 5425 &

# Push a small file. First 2s should work; after that all packets dropped.
csh -i init/zmq.csh "satdeploy push hello -n 5425"
```

Expected behavior: push starts normally, then completely fails after 2 seconds. Agent log should show:

```
[loss_filter] loaded 2 events from /tmp/test_pattern.pattern
...
[loss_filter] final stats: dropped X of Y packets (Z%)
```

If you see this, the filter is wired correctly and you can start running F3/F4/F5 experiments.

## 6. Operational reminder

**The flight build must NEVER include this.** Belt-and-braces:

- Yocto recipes for the satellite build should not pass `-Dtest_loss_filter=true`.
- CI or pre-flight check: confirm the flight binary has no `loss_filter` symbols.
  ```bash
  nm satdeploy-agent | grep -i loss_filter
  # Should print nothing.
  ```
- Code review checklist: any change to `loss_filter.c` requires confirmation that flight builds are unaffected. The file should be small enough that this stays easy to verify.

---
name: Bug report
about: Something that satdeploy did wrong (bad behaviour, crash, wire-format mismatch, hung transfer, etc.)
title: "[bug] "
labels: bug
assignees: ''
---

### What happened
<!-- One sentence. What you expected, what you got. -->

### How to reproduce
<!--
Step-by-step. Paste the exact `satdeploy ...` invocation. If it's an agent-side
bug, paste the agent command line too (`satdeploy-agent -i ZMQ -p localhost -a 5425`).
-->

```
satdeploy push controller
satdeploy status
```

### Output / logs
<!--
Paste relevant output. The agent prefixes its log lines with subsystem tags
(`[deploy]`, `[dtp]`, `[session_state]`); please include those. For service
logs on the target: `satdeploy logs <app> -l 100`.
-->

```
[paste here]
```

### Environment

- satdeploy-apm version: <!-- `satdeploy version` inside csh -->
- satdeploy-agent version: <!-- `satdeploy-agent --version` on target -->
- CSH version / commit: <!-- `csh --version` -->
- libcsp / libdtp commits used to build: <!-- `git -C lib/csp rev-parse HEAD` etc. -->
- Transport: <!-- ZMQ / CAN / KISS -->
- Target architecture: <!-- armv8a / x86_64 / ... -->
- Host OS: <!-- Ubuntu 24.04 / Debian 12 / ... -->

### Cross-pass resume context (if applicable)

<!-- Only fill in if the bug involves resuming a partial transfer. -->

- Sidecar present at `/var/lib/satdeploy/state/<app>.dtpstate`? <!-- ls output -->
- Sidecar size + mode:
- Was the binary re-staged (different SHA256) between attempts? <!-- yes / no -->

### Anything else
<!-- Workarounds you've tried, suspected root cause, related issues. -->

---
name: Feature request
about: A capability satdeploy should have but doesn't
title: "[feat] "
labels: enhancement
assignees: ''
---

### The job
<!--
Describe the operational job you're trying to do, not the implementation.
"I want to push 12 binaries to 4 satellites in one pass window" is better than
"add a --batch flag".
-->

### How it works today
<!--
What you do now. If you've worked around the gap, paste the workaround — that
tells us where the friction lives.
-->

### What "good" looks like
<!--
A sketch of the command, output, or behaviour. CLI invocation if it's a CLI
change; sidecar/wire format diff if it's a protocol change.
-->

```
satdeploy <imagined-command> ...
```

### Constraints we should know about

- Flight-software constraint: <!-- e.g. must be backwards-compatible with v0.4.0 sidecar -->
- Pass-window constraint: <!-- must complete in <5 min? must survive Ctrl-C? -->
- Hardware constraint: <!-- ARM target memory, KISS bandwidth, etc. -->

### Alternatives considered
<!-- What you ruled out and why. -->

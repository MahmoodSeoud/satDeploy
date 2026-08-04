# Try it right now

Ten minutes, copy-paste, nothing to install except Docker. Every command and
every output below was run exactly as written.

You will: deploy a file to a (simulated) satellite, break the radio link on
purpose, watch the deploy survive it, then cut the satellite off mid-transfer
and watch it pick up where it left off on the next pass.

---

## Before you start

Docker must be running. Check:

```sh
docker ps
```

If that prints a table (even an empty one), you are fine. If it says "Cannot
connect to the Docker daemon", open Docker Desktop and wait for it to start.

---

## Step 1 — Start it

```sh
cd ~/projects/satDeploy
./scripts/demo.sh
```

**You should see:**

```
>>> Starting container satdeploy-demo
>>> Building the satellite agent and the ground station...
>>> Starting zmqproxy
>>> Building the test instruments (csp_loss + csp_monitor)...
>>> Ready. Opening the lab...
```

The very first run takes about 5 minutes (it compiles everything). Every run
after that takes seconds. When it finishes, your terminal splits in two.

---

## Step 2 — Understand what you are looking at

```
+---------------------------------+---------------------------------+
|  LEFT = GROUND STATION          |  RIGHT = THE SATELLITE          |
|  You type here.                 |  You only read this.            |
|                                 |                                 |
|  csh #                          |  Agent running.                 |
+---------------------------------+---------------------------------+
```

- **Type in the left pane.** That is the operator console.
- **Watch the right pane.** That is the spacecraft telling you what it is doing.
- To click into the other pane: `Ctrl-b` then `o`.

A short cheat sheet is printed in the left pane at startup. This page is the
long version.

Four test files are already staged for you:

| Name | What it is | Use it for |
|------|-----------|-----------|
| `script-79b` | a real shell script | instant, first try |
| `binary-100kb` | random bytes | the loss demo |
| `binary-5mb` | random bytes | the interrupted-pass demo |
| `binary-50mb` | random bytes | when you want it to take a while |

The name states the size, so choosing one needs no lookup.

They live in `/apps/`.

---

## Step 3 — Deploy something

In the **left** pane:

```
satdeploy status
```

**You should see:**

```
Target: node 5425

No apps deployed.
```

Nothing is up there yet. (You never type the satellite's address: the lab has
one satellite and sets it as the default. `-n <addr>` overrides it if you ever
need to.) Now send a file:

```
satdeploy push /apps/script-79b /target/script-79b
```

**Left pane:**

```
checking node 5425...
/apps/script-79b -> node 5425:/target/script-79b  (79 bytes, 69db828b)
  waiting for the satellite to verify...
> Deployed script-79b (69db828b) over SVU
```

**Right pane, at the same time:**

```
[deploy] script-79b -> deploying
svu: VERIFIED 79 bytes in 1 round(s)
[deploy] checksum ok: 69db828b
[deploy] script-79b -> installed at /target/script-79b
```

**VERIFIED is the whole point.** The satellite said that, not the ground. It
hashed every block of the file against a manifest before writing anything to
disk. If even one byte were wrong it would refuse to install instead of
reporting success.

`push` reads like `scp`: **source, then destination**, then which satellite.

```
satdeploy push  <file here>  <where it goes up there>
```

There is no app name to invent. The name you see in `status` and `list`
(`script-79b`) is just the destination's filename, so the path is the only thing
identifying an app. That is why the other commands take the same path:

```
satdeploy list     /target/script-79b
satdeploy rollback /target/script-79b
```

### Undo a bad deploy

Push something different to the same place, as if you had just shipped a broken
build:

```
satdeploy push /apps/binary-100kb /target/script-79b
```

Now look at what the satellite is holding:

```
satdeploy list /target/script-79b
csp_link test 5425 -c 10
```

```
    HASH      	TIMESTAMP           	STATUS  	SIZE      	PATH
    ----------------------------------------------------------------
  > fa5faab7  	2026-08-02T08:40:26 	deployed	100.0 KB  	...
  * 69db828b  	2026-08-02T08:40:22 	backup  	79 B      	...
```

`>` is what is running. `*` is an older version still on board. Before
installing anything, the satellite copies whatever was already there into its
own backup folder, so the previous build never left the spacecraft. Go back:

```
satdeploy rollback /target/script-79b
```

```
> Rolled back script-79b to 69db828b
```

Run `satdeploy list /target/script-79b` again and the `>` has moved back to
`69db828b`.

**Why this matters more in orbit than on a server:** fixing a bad deploy by
uploading a good one costs an entire pass, and the next pass might be hours
away. Rolling back costs no radio time at all, because the good build is
already up there. `-H <hash>` picks a specific older version instead of the
most recent.

---

## Step 4 — Ask how good the link is

Before breaking anything, measure what you have. This has nothing to do with
files: it probes the link itself, so it works against any CSP node.

```
csp_link test 5425 -c 10
```

```
Link to node 5425
  loss        0.0 %  (0 of 10 probes never returned)
  rtt         min 3 ms, median 3 ms, mean 3 ms, max 4 ms
  jitter      1 ms mean change between probes
  worst burst 0 consecutive losses

  Verdict: clean. Nothing was dropped.
```

This is the command to run first against a real spacecraft, before deciding
whether this pass is worth attempting an upload on.

---

## Step 5 — Break the radio link

This is the interesting part. Still in the left pane:

```
csp_loss start -L 0.10
```

**You should see:**

```
csp_loss: 10.0% loss on ZMQ0 (TX from this node)
csp_loss: independent per-transmission loss
```

One in ten packets now disappears. The link stays broken until you say
otherwise. Deploy across it anyway:

```
satdeploy push /apps/binary-100kb /target/binary-100kb
```

It takes a few seconds longer than before. **Left pane still ends with:**

```
> Deployed binary-100kb (1887951f) over SVU
```

**But the right pane shows the work it took:**

```
svu: round 2 -> INCOMPLETE (8 frame(s) accepted, 2 bad range(s), first [57912,59944)), re-requesting 2
svu: round 3 -> INCOMPLETE (2 frame(s) accepted, 1 bad range(s), first [58928,59944)), re-requesting 1
svu: VERIFIED 102400 bytes in 4 round(s)
```

Read that: each round the satellite worked out exactly which byte ranges were
missing and asked for **only those**, not the whole file again. Four rounds
later everything verified.

Your numbers will differ from mine. Loss is random, so the round count and the
byte ranges change every run. That is the point.

---

## Step 6 — Look at the damage, then fix the link

```
csp_loss status
```

**You should see something like:**

```
csp_loss: active on ZMQ0 -- target 10.0%, mode independent
csp_loss: offered 130, dropped 14, delivered 116 (10.77% actual)
```

Asked for 10%, actually dropped 10.77%. Measure it the way you would a real
link, from the outside:

```
csp_link test 5425 -c 40
```

```
  loss        17.5 %  (7 of 40 probes never returned)
  worst burst 2 consecutive losses

  Verdict: lossy. Expect several recovery rounds; a transport
           without recovery will struggle.
```

Now repair the link:

```
csp_loss stop
```

```
csp_loss: stopped on ZMQ0 -- offered 130, dropped 14 (10.77%)
```

**Try this:** run Step 5 again with `-L 0.30` instead of `-L 0.10` and watch the
round count climb. Somewhere above 30% it stops finishing at all, and reports
failure rather than installing a broken file.

---

## Step 7 — Cut the satellite off mid-transfer

A real satellite pass ends whether or not your upload finished. This is the
part nothing else does.

Start a big transfer in the **left** pane:

```
satdeploy push /apps/binary-5mb /target/binary-5mb
```

While it is running (about 10 seconds), **switch to the right pane** (`Ctrl-b`
then `o`) and press **`Ctrl-C`**. The satellite is now out of range.

See what survived — press `Ctrl-b` then `o` to get back to the left pane, and
run:

```
satdeploy status
```

It will fail to answer, because you just turned the satellite off. That is
correct. Now look at the satellite's own disk. Ctrl-C left the **right** pane at
a shell prompt inside the satellite, so just run it there:

```sh
ls -la /var/lib/satdeploy/state/
```

```
-rw------- 1 root root 2744232 binary-5mb.svupart
```

**2.7 MB of the 5 MB file is still up there**, saved as it arrived.

Now the next pass begins. In the **right** pane, press the up-arrow and Enter to
restart the agent. Then in the **left** pane run the exact same push again:

```
satdeploy push /apps/binary-5mb /target/binary-5mb
```

**Right pane:**

```
svu: restored 2744232 bytes from a prior pass
svu: round 1 -> CORRUPT (1 bad range(s), first [2744232,5242880)), re-requesting 1
svu: VERIFIED 5242880 bytes in 2 round(s)
```

It restored what it already had, asked only for the missing tail, and verified
the result. It re-checked the restored bytes against a fresh manifest first, so
if you had rebuilt the file on the ground in between, it would have noticed and
refetched instead of trusting stale data.

---

## Step 8 — Stop, and start over

To quit: `Ctrl-C` in the right pane, then type `exit` in the left pane.

To start again from scratch:

```sh
docker rm -f satdeploy-demo
./scripts/demo.sh
```

---

## If something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `Aborting: no reply to the pre-flight check` | The satellite is not answering | Check the right pane says `Agent running.` If you Ctrl-C'd it, restart it (up-arrow, Enter) |
| `Already deployed: script-79b` | Same file is already up there | Add `-F` to force it |
| `Error: No remote path specified` | You forgot `-r` | The error prints the full corrected command; copy it |
| `No such command` | Typo | Type `satdeploy`, `csp_loss` or `csp_monitor` on their own to see what they accept |
| `SVU transfer did not verify` | Too much loss to recover | Expected above ~30%. Nothing was installed, which is the designed behaviour |
| `cannot open ... for writing` | The folder on the satellite does not exist | `-r` is a file path and its parent folder must exist |
| Terminal looks frozen | It is waiting on the satellite | A push always prints what it is doing before it waits. No output at all means the command was never accepted |
| Panes are confusing | tmux | `Ctrl-b` then `o` switches. `Ctrl-b` then `z` zooms one pane full screen; press again to unzoom |

---

## The whole thing, to copy-paste

```
satdeploy status
satdeploy push /apps/script-79b /target/script-79b
csp_loss start -L 0.10
satdeploy push /apps/binary-100kb /target/binary-100kb
csp_loss status
csp_loss stop
satdeploy list /target/script-79b
csp_link test 5425 -c 10
```

---

## What next

- `docs/MANUAL.md` — the full reference: recording the wire with `csp_monitor`,
  rollback and version history, the two loss modes, and reproducible
  measurement runs with the experiment harness.
- Type `satdeploy`, `csp_loss`, or `csp_monitor` bare at the prompt for their
  own built-in help, or add `-h` to any subcommand.

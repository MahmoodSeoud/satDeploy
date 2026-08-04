# satdeploy — user manual

Deploy software to a satellite over a link that loses packets, and know for
certain whether what landed is what you sent.

This manual is the whole tool: start it, use it, break the link on purpose,
and read what comes back. Everything here has been run; nothing is aspirational.

**First time? Read `docs/QUICKSTART.md` instead** -- same thing, ten minutes,
every command copy-pasteable with the output you should see. Come back here for
the reference.

---

## 1. Start it

One command, from a Mac or Linux box with Docker:

```sh
cd ~/projects/satDeploy
./scripts/demo.sh
```

First run takes a few minutes (it builds CSH, libcsp, the agent, the plugins).
After that it is seconds. You land in a split screen:

```
+-------------------------------+-------------------------------+
| GROUND STATION (csh)          | SATELLITE (satdeploy-agent)   |
| you type here                 | it reports what it is doing   |
| csh #                         | Agent running.                |
+-------------------------------+-------------------------------+
```

The left pane is the operator console. The right pane is the spacecraft: you
do not type there, you read it. A cheat sheet prints in the left pane at
startup; this manual is the long form.

Move between panes with `Ctrl-b o`. Quit with `Ctrl-C` in the right pane, then
`exit` on the left.

Four test files are staged for you, named for what they are: `script-79b` (a
real shell script), `binary-100kb`, `binary-5mb`, `binary-50mb`, all under
`/apps/`.

---

## 2. Deploy something

```
satdeploy push /apps/script-79b /target/script-79b
```

Source, then destination -- the same shape as `scp`. There
is no app-name argument: an app is identified by its path on the target, and the
short name in `status`/`list` output is that path's basename. `-f`/`-r` are still
accepted as flag equivalents of the two positionals, for existing scripts.

The right pane narrates the transfer:

```
svu_transfer: pulling from node 19 -> /target/script-79b.tmp (mtu 1024, block 4096, max 24 round(s))
svu: VERIFIED 79 bytes in 1 round(s)
[deploy] checksum ok: 3f9535cf
```

**VERIFIED is the word that matters.** The satellite said it, not the ground.
It hashed every block against a manifest the ground sent ahead of the data, and
it only writes the file after every block matches. A transfer that cannot say
VERIFIED fails loudly instead of installing something broken.

Other commands:

```
satdeploy status                    what is deployed right now
satdeploy list /target/script-79b                version history for one app
satdeploy rollback /target/script-79b
satdeploy logs /target/script-79b                the app's log tail (needs systemd
                                            on the target; the Docker lab has
                                            no journalctl, so this one errors)
```

Pushing the same bytes twice is refused as a no-op. `-F` forces it, which is
what you want when demonstrating repeatedly.

### Versions and rollback

Before the agent installs anything it copies the file currently at that remote
path into `/opt/satdeploy/backups/<app>/<timestamp>-<sha256>.bak`, so the
version you are replacing stays on the spacecraft. `list` shows them:

```
  > fa5faab7  	2026-08-02T08:40:26 	deployed	100.0 KB  	...
  * 69db828b  	2026-08-02T08:40:22 	backup  	79 B      	...
```

`>` is deployed, `*` is an available backup. `satdeploy rollback /target/script-79b`
restores the most recent one; `-H <hash>` picks a specific version. Restoring
preserves the backup's file permissions, and the copy removes the destination
first so a running binary does not fail with `ETXTBSY`.

Backups are deduplicated by content hash, so re-pushing identical bytes does not
create a second copy. They are **not** pruned, though: every distinct version
you push stays on the target indefinitely. The old `max_backups` setting went
away with the config file in v2, and no retention logic replaced it. On a
flash-constrained board that is a real limit worth knowing about before flight.

The point of all this is radio time. Recovering from a bad deploy by uploading a
good build costs a whole pass; rolling back costs none, because the previous
build never left the satellite.

---

## 3. Break the link on purpose

This is the point of the project, and it is one command:

```
csp_loss start -L 0.10
```

```
csp_loss: 10.0% loss on ZMQ0 (TX from this node)
csp_loss: independent per-transmission loss
```

Ten percent of everything the ground transmits now disappears. The link stays
broken until you say otherwise, and you can deploy across it as normal:

```
satdeploy push /apps/binary-100kb /target/binary-100kb -F
```

Right pane, the recovery loop working:

```
svu: round 1 -> INCOMPLETE (88 frame(s) accepted, 10 bad range(s), first [17272,20320)), re-requesting 10
svu: VERIFIED 102400 bytes in 2 round(s)
```

Round 1 lost ten stretches of the file. Round 2 asked for **only those ten
stretches** — not the whole file again — and then verified. That is the
difference between a design that recovers and one that restarts.

Check the damage, then repair the link:

```
csp_loss status
csp_loss stop
```

```
csp_loss: active on ZMQ0 -- target 10.0%, mode independent
csp_loss: offered 130, dropped 14, delivered 116 (10.77% actual)
```

Things to try: raise `-L` to `0.3` and watch the round count climb. Push
`binary-5mb` instead and watch it grind. Turn loss on *mid-transfer* from
a second pane and watch the protocol notice.

### The two loss modes

`csp_loss start -L 0.1` gives **independent loss**: every transmission gets a
fresh coin flip, so a re-sent packet has a fresh chance to make it. That is what
a radio link does, and it is the mode to use for anything you are watching.

`csp_loss start -L 0.1 -M 9 -S 42 -o drops.csv` gives **replayable loss**: the
drop decision is keyed to the packet's identity, so the same seed reproduces the
same drop set exactly, run after run, and `drops.csv` records every decision.
That is the mode for a measurement you intend to publish.

They are not interchangeable. Under `-M`, a re-sent fragment is dropped *again*,
every time, so a retrying protocol can never finish. That is correct behaviour
for a fixed drop set and useless for a live demo. The command warns you when you
pick it.

---

## 3b. Measure the link itself

`csp_loss` and the transfer both tell you about *your* traffic. To ask how good
a link is in general -- no file, no deployment, nothing installed on the far
end -- probe it:

```
csp_link test 5425            # 20 probes
csp_link test 5425 -c 50      # more probes, tighter estimate
csp_link test 5425 -s 500     # bigger packets, where fragmentation bites
csp_link test 5425 -i 1000    # slower, for a real radio
```

```
Link to node 5425
  loss        17.5 %  (7 of 40 probes never returned)
  rtt         min 3 ms, median 3 ms, mean 4 ms, max 9 ms
  jitter      2 ms mean change between probes
  worst burst 2 consecutive losses

  Verdict: lossy. Expect several recovery rounds; a transport
           without recovery will struggle.
```

It probes with CSP ping, which every CSP node answers, so it works against a
flatsat, a spacecraft on a pass, or a lab bus without deploying anything. One
probe is one packet each way, so probe loss is the round-trip loss: a probe is
missing if either the request or the reply was dropped.

`worst burst` is there because an average hides the shape of the loss. Ten
percent spread evenly and ten percent arriving four-in-a-row demand very
different retry budgets, and only the second one breaks a transport that
retries three times.

---

## 4. Record what actually crossed the wire

`csp_loss` says what the ground *tried* to send. `csp_monitor` is an independent
witness that records what actually appeared on the link:

```
csp_monitor start -d 9 -o /tmp/wire.csv
... run a push ...
csp_monitor stop
```

`-d 9` scopes it to the bulk transfer port. Then, from any shell
(`docker exec -it satdeploy-demo bash`):

```sh
head /tmp/wire.csv
```

```
1143994,19,5425,9,0x00,0,,,,0,0,,
1143995,19,5425,9,0x00,0,,,,1016,5,,
```

One row per frame: timestamp, source, destination, port, and the byte offset
each fragment carried. Two independent records — what was dropped and what
crossed — is what makes a claim about the link trustworthy rather than
anecdotal. Neither is the sender's own opinion of itself.

---

## 5. Survive a lost pass

A real satellite pass ends whether or not your upload finished. This is the
part of the tool that a whole-file checksum cannot do.

Start a big transfer:

```
satdeploy push /apps/binary-5mb /target/binary-5mb
```

While it is running (about ten seconds), press **Ctrl-C in the right pane**.
That is the pass window closing mid-upload. Check what survived, from any shell:

```sh
ls -la /var/lib/satdeploy/state/
```

```
-rw------- 1 root root 2691400 binary-5mb.svupart
```

Everything received so far is on the satellite's disk. Restart the agent in the
right pane (up-arrow, Enter) — this is the next pass — and push the same line
again:

```
svu: restored 2691400 bytes from a prior pass
svu: round 1 -> CORRUPT (1 bad range(s), first [2691072,5242880)), re-requesting 1
svu: VERIFIED 5242880 bytes in 2 round(s)
```

Only the missing tail crossed the link. The satellite re-verified the restored
bytes against a *fresh* manifest before trusting them, so if the ground had
rebuilt the artifact in between, the stale data would have been detected and
re-fetched instead of silently kept.

---

## 6. When something goes wrong

**`Aborting: no reply to the pre-flight check on node N`** — the satellite did
not answer within 15 s. Either the agent is not running (check the right pane),
or the link ate the request: the command channel is a single unprotected packet
each way, so at 10% loss it fails roughly one time in five. Push again, or pass
`-F` to skip the check entirely. (The unprotected command channel is a real gap
in the tool, not a lab artifact.)

**`SVU transfer did not verify`** — the transfer never reached full block
verification within its 24 recovery rounds. Expected above roughly 30% loss.
Nothing is installed: refusing is the designed outcome.

**`svu: cannot open ... for writing`** — the destination directory does not
exist on the satellite. `-r` names a file path, and its parent must exist.

**`Error: version mismatch` / no-op push** — the same bytes are already
deployed. Add `-F`.

**Nothing happens at all** — check the right pane is actually running (`Agent
running.`); if you Ctrl-C'd it earlier, restart it there. A push always prints
what it is doing before it waits, so a console with no output means the command
was never accepted, not that it is working silently.

**You forgot a command's name** — type it bare. `satdeploy`, `csp_loss` and
`csp_monitor` each print their own usage; add `-h` to any subcommand
(`csp_loss start -h`) for the full option list.

---

## 7. Reproducible measurement runs

For numbers rather than demonstrations, the experiment harness runs one
transfer per invocation and appends one CSV row:

```sh
docker exec -it satdeploy-demo bash
cd /satdeploy
experiments/harness.sh --experiment e1 --size 102400 --seed 42 \
  --csv /tmp/results.csv --link zmq
```

Useful flags: `--link kiss --loss-pct 10` (radio-style frame loss),
`--kill-at-byte N --max-passes 2` (cut the pass mid-transfer),
`--rate-bps N` (throttle to a radio rate). Sweeps live in
`experiments/runs/e{1,2,4}_*.sh`.

The control build — same source, recovery and resume compiled out — is what
isolates the mechanism:

```sh
AGENT_BIN=/root/builds/agent-naive/satdeploy-agent experiments/harness.sh ...
```

`./scripts/demo.sh auto` runs a four-case self-check across both builds if you
just want to confirm a fresh clone still works.

---

## 8. What is where

| Path | What |
|------|------|
| `scripts/demo.sh` | starts the lab |
| `init/lab.csh` | the ground station's startup script |
| `satdeploy-agent/` | the satellite side |
| `satdeploy-apm/` | the ground side (a csh plugin) |
| `*/svu/` | the transfer layer: manifest, verification, recovery |
| `experiments/` | the measurement harness |
| `/var/lib/satdeploy/state/` | resume sidecars, on the satellite |
| `~/projects/csp-intercept` | the instrument: `csp_loss`, `csp_monitor` |

Loss injection and wire monitoring come from the csp-intercept repo, mounted
automatically when it sits beside this one. Its own manual is
`csp-intercept/docs/HOWTO.md`.

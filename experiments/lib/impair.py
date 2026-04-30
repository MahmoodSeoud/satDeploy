#!/usr/bin/env python3
"""
KISS-aware pty-pty link with byte- and frame-level impairment for the
satdeploy experiment harness. Models a UHF/serial KISS link the way it
actually behaves: the radio sends and receives whole AX.25/KISS frames,
fades drop frames as units, and bit errors corrupt within a frame.

The link looks like:

    agent process  <->  /tmp/agent_pty  <->  [impair.py]  <->  /tmp/ground_pty  <->  csh

We open two openpty() pairs — one per side. Each side's slave is symlinked
to a well-known path (default /tmp/agent_pty, /tmp/ground_pty). Each side's
master fd is held by impair.py. Two threads forward bytes between the two
masters, applying impairment.

Why frame-level (not byte-level) by default
-------------------------------------------
A UHF radio drops a *frame* when its FEC fails or carrier is lost during
that packet. Dropping individual bytes inside a frame is unrealistic:
KISS will just drop the corrupted frame anyway because the FEND will be
missing or escape sequences will mismatch. So default loss = frame-level.
Byte-corruption (bit-flips) is still byte-level — that's how channel BER
manifests when the FEC manages to recover the frame boundary.

KISS framing — for parsing frames out of the byte stream
--------------------------------------------------------
RFC-style; same as SLIP.
    FEND  = 0xC0   frame delimiter (begin/end)
    FESC  = 0xDB   escape
    TFEND = 0xDC   transposed FEND (escaped)
    TFESC = 0xDD   transposed FESC (escaped)

We do NOT need to *interpret* KISS commands or unescape — only to know
where frame boundaries are so frame-level loss drops complete frames. The
inter-frame bytes (FEND padding, idle) are forwarded unchanged.

Loss models
-----------
- Bernoulli (--loss-pct):
    Each frame is dropped independently with probability loss_pct.
- Gilbert-Elliott (--ge-p, --ge-r, --ge-loss-good, --ge-loss-bad):
    Two-state Markov chain. State G has loss prob loss_good; state B has
    loss_bad. p = P(G->B), r = P(B->G). Mean burst length = 1/r.
    This is the standard channel model for bursty radio links. Cite:
    Gilbert (1960), Elliott (1963).

Throughput throttling (--rate-bps)
----------------------------------
Models UHF channel rate. For raw 9600 bps GMSK, bytes/sec ~ rate_bps / 8
(no start/stop bits — bits are in the channel coder, not asynch UART).
For UART-style synchronous KISS, 10 bits/byte is more accurate. We use
divisor 8 by default; --bits-per-byte overrides for UART models.

Reproducibility
---------------
--seed seeds Python's random module so a given (seed, params) reproduces
the same drop/corrupt sequence across runs. Required for thesis.
"""

import argparse
import os
import random
import select
import signal
import sys
import termios
import threading
import time

FEND = 0xC0
FESC = 0xDB
# TFEND/TFESC don't appear here — we only care about frame boundaries.

# --------------------------------------------------------------------------
# pty setup
# --------------------------------------------------------------------------

def make_pty_pair(symlink_path):
    """Create a pty pair, symlink the slave to symlink_path, return master fd.

    The slave is set raw (cfmakeraw) so libcsp's termios calls don't munge
    binary data (no echo, no canonical line buffering, no ^D handling).
    """
    master_fd, slave_fd = os.openpty()

    attrs = termios.tcgetattr(slave_fd)
    # cfmakeraw — Python doesn't expose it, do it manually.
    # Reference: man 3 termios
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
    iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK |
               termios.ISTRIP | termios.INLCR | termios.IGNCR |
               termios.ICRNL | termios.IXON)
    oflag &= ~termios.OPOST
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON |
               termios.ISIG | termios.IEXTEN)
    cflag &= ~(termios.CSIZE | termios.PARENB)
    cflag |= termios.CS8
    cc[termios.VMIN] = 1
    cc[termios.VTIME] = 0
    termios.tcsetattr(slave_fd, termios.TCSANOW,
                      [iflag, oflag, cflag, lflag, ispeed, ospeed, cc])

    slave_path = os.ttyname(slave_fd)
    # Replace any prior symlink
    try:
        os.unlink(symlink_path)
    except FileNotFoundError:
        pass
    os.symlink(slave_path, symlink_path)
    # Hold slave_fd open for the lifetime of the process — closing it
    # makes new opens of the slave path return ENXIO on some kernels and
    # EOF on others, neither of which we want.
    return master_fd, slave_fd


# --------------------------------------------------------------------------
# Loss models
# --------------------------------------------------------------------------

class BernoulliLoss:
    def __init__(self, loss_pct, rng):
        self.loss_pct = loss_pct
        self.rng = rng

    def drop(self):
        return self.rng.random() * 100 < self.loss_pct

    def state_label(self):
        return "B"  # Bernoulli — single state


class GilbertElliottLoss:
    """Two-state Markov-modulated loss.
    Good (G):  loss with prob (1 - k) = loss_good
    Bad  (B):  loss with prob (1 - h) = loss_bad
    p:  G -> B transition
    r:  B -> G transition
    """
    def __init__(self, p, r, loss_good, loss_bad, rng):
        self.p = p / 100.0
        self.r = r / 100.0
        self.loss_good = loss_good / 100.0
        self.loss_bad = loss_bad / 100.0
        self.rng = rng
        self.in_bad = False

    def drop(self):
        # Step 1: state transition.
        if self.in_bad:
            if self.rng.random() < self.r:
                self.in_bad = False
        else:
            if self.rng.random() < self.p:
                self.in_bad = True
        # Step 2: emit loss decision based on (post-transition) state.
        loss_p = self.loss_bad if self.in_bad else self.loss_good
        return self.rng.random() < loss_p

    def state_label(self):
        return "B" if self.in_bad else "G"


# --------------------------------------------------------------------------
# Frame splitter — yields complete KISS frames + inter-frame slack.
# --------------------------------------------------------------------------

class KissFramer:
    """Stateful byte stream -> frame chunks.

    Yields (kind, bytes):
        ("idle", b)   — bytes outside any frame (incl. lone FENDs that
                        delimit empty frames or padding). Always forwarded.
        ("frame", b)  — a complete KISS frame including its trailing FEND.
                        Subject to frame-level loss.

    A KISS frame here = bytes between (and including) the opening FEND
    and the closing FEND. Empty frames (FEND immediately followed by
    FEND) are treated as idle and not dropped. We do NOT unescape — the
    receiver does that — but we track FESC so a FEND inside an escape
    sequence isn't mistaken for a frame end.
    """
    def __init__(self):
        self.in_frame = False
        self.escaped = False
        self.buf = bytearray()
        self.idle_buf = bytearray()

    def feed(self, data):
        for b in data:
            if not self.in_frame:
                if b == FEND:
                    # Could be start of frame OR padding between frames.
                    # We can't tell yet — consume FEND and switch state.
                    # If the next byte is also FEND it's an empty frame
                    # (idle). Buffer the lead FEND in `buf` so it goes
                    # with the frame if one starts; flush as idle if not.
                    self.in_frame = True
                    self.buf = bytearray([FEND])
                else:
                    self.idle_buf.append(b)
            else:
                # In frame. Track escape so a FEND in escape doesn't
                # close the frame.
                self.buf.append(b)
                if self.escaped:
                    self.escaped = False
                    continue
                if b == FESC:
                    self.escaped = True
                    continue
                if b == FEND:
                    # Frame end. Decide: was it empty (FEND FEND) -> idle,
                    # or non-empty -> a real frame.
                    if len(self.buf) == 2:
                        # FEND FEND with nothing between — idle padding.
                        if self.idle_buf:
                            yield ("idle", bytes(self.idle_buf))
                            self.idle_buf = bytearray()
                        yield ("idle", bytes(self.buf))
                    else:
                        if self.idle_buf:
                            yield ("idle", bytes(self.idle_buf))
                            self.idle_buf = bytearray()
                        yield ("frame", bytes(self.buf))
                    self.buf = bytearray()
                    self.in_frame = False

    def drain_idle(self):
        """Flush any accumulated idle bytes (e.g. on shutdown)."""
        if self.idle_buf:
            out = bytes(self.idle_buf)
            self.idle_buf = bytearray()
            return out
        return b""


# --------------------------------------------------------------------------
# Forwarder
# --------------------------------------------------------------------------

class Forwarder:
    def __init__(self, src_fd, dst_fd, name, args, rng):
        self.src_fd = src_fd
        self.dst_fd = dst_fd
        self.name = name
        self.args = args
        self.bytes_per_sec = (args.rate_bps / args.bits_per_byte
                              if args.rate_bps else 0)
        self.framer = KissFramer()

        # Model selection: GE only when --ge-p is explicitly non-zero. The
        # other GE knobs have non-zero defaults (e.g. ge_loss_bad=100), so
        # we must NOT key on those — that would silently override Bernoulli
        # loss with a misconfigured GE that never enters the bad state.
        if args.ge_p > 0:
            self.loss = GilbertElliottLoss(
                args.ge_p, args.ge_r,
                args.ge_loss_good, args.ge_loss_bad, rng)
        else:
            self.loss = BernoulliLoss(args.loss_pct, rng)

        # Stats
        self.frames_seen = 0
        self.frames_dropped = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.last_send = time.monotonic()

    def maybe_corrupt(self, frame_bytes):
        if self.args.corrupt_pct <= 0:
            return frame_bytes
        out = bytearray(frame_bytes)
        for i in range(len(out)):
            # Don't corrupt FEND/FESC themselves — that would change
            # the frame structure under us, which isn't what BER does.
            # BER models the link: a flipped bit looks like a corrupted
            # data byte to the receiver.
            if out[i] in (FEND, FESC):
                continue
            if random.random() * 100 < self.args.corrupt_pct:
                out[i] ^= (1 << random.randint(0, 7))
        return bytes(out)

    def throttle(self, n_bytes):
        if self.bytes_per_sec <= 0:
            return
        now = time.monotonic()
        ideal = self.last_send + n_bytes / self.bytes_per_sec
        if ideal > now:
            time.sleep(ideal - now)
        self.last_send = max(self.last_send, time.monotonic())

    def latency(self):
        if self.args.latency_ms <= 0:
            return
        jitter = (random.uniform(-1, 1) * self.args.jitter_ms
                  if self.args.jitter_ms > 0 else 0)
        time.sleep(max(0, (self.args.latency_ms + jitter) / 1000.0))

    def write_out(self, data):
        if not data:
            return
        self.throttle(len(data))
        self.latency()
        try:
            os.write(self.dst_fd, data)
            self.bytes_out += len(data)
        except OSError as e:
            print(f"[{self.name}] write err: {e}", file=sys.stderr,
                  flush=True)

    def run(self):
        while True:
            try:
                # select with a small timeout so we can notice shutdown
                # and so periodic stats can still print if we add them.
                rlist, _, _ = select.select([self.src_fd], [], [], 1.0)
                if not rlist:
                    continue
                data = os.read(self.src_fd, 4096)
            except OSError as e:
                print(f"[{self.name}] read err: {e}", file=sys.stderr,
                      flush=True)
                return
            if not data:
                continue
            self.bytes_in += len(data)

            for kind, chunk in self.framer.feed(data):
                if kind == "idle":
                    self.write_out(chunk)
                else:  # "frame"
                    self.frames_seen += 1
                    if self.loss.drop():
                        self.frames_dropped += 1
                        if self.args.verbose:
                            print(f"[{self.name}] drop frame "
                                  f"(state={self.loss.state_label()}) "
                                  f"len={len(chunk)}",
                                  file=sys.stderr, flush=True)
                        continue
                    self.write_out(self.maybe_corrupt(chunk))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent-link", default="/tmp/agent_pty",
                    help="Symlink path for the agent's pty (default: /tmp/agent_pty)")
    ap.add_argument("--ground-link", default="/tmp/ground_pty",
                    help="Symlink path for the ground's pty (default: /tmp/ground_pty)")

    # Bernoulli loss
    ap.add_argument("--loss-pct", type=float, default=0,
                    help="Independent per-frame drop probability (0-100)")

    # Gilbert-Elliott
    ap.add_argument("--ge-p", type=float, default=0,
                    help="Gilbert-Elliott P(G->B) percent. If non-zero, GE is used.")
    ap.add_argument("--ge-r", type=float, default=0,
                    help="Gilbert-Elliott P(B->G) percent.")
    ap.add_argument("--ge-loss-good", type=float, default=0,
                    help="Loss prob in Good state (default 0)")
    ap.add_argument("--ge-loss-bad", type=float, default=100,
                    help="Loss prob in Bad state (default 100)")

    # Other impairment
    ap.add_argument("--corrupt-pct", type=float, default=0,
                    help="Per-byte bit-flip probability (0-100), excludes FEND/FESC")
    ap.add_argument("--rate-bps", type=int, default=0,
                    help="Channel rate in bits/sec (0 = unthrottled). "
                         "9600 models UHF baseline.")
    ap.add_argument("--bits-per-byte", type=int, default=8,
                    help="Effective bits per byte for throttling (8 for synchronous, "
                         "10 for UART 8-N-1).")
    ap.add_argument("--latency-ms", type=int, default=0,
                    help="One-way propagation delay")
    ap.add_argument("--jitter-ms", type=int, default=0,
                    help="Latency jitter (uniform [-jitter, +jitter])")

    ap.add_argument("--seed", type=int, default=None,
                    help="RNG seed (default: time-based, non-reproducible)")
    ap.add_argument("--verbose", action="store_true",
                    help="Log every dropped frame to stderr")
    ap.add_argument("--ready-file", default=None,
                    help="If set, touch this path once both ptys are up")

    args = ap.parse_args()

    rng = random.Random(args.seed)
    # Also seed module-level random for corruption (uses random.randint)
    if args.seed is not None:
        random.seed(args.seed)

    # Bring up the two pty pairs.
    master_a, slave_a = make_pty_pair(args.agent_link)
    master_b, slave_b = make_pty_pair(args.ground_link)

    print(f"impair: agent={args.agent_link} -> {os.ttyname(slave_a)}",
          flush=True)
    print(f"impair: ground={args.ground_link} -> {os.ttyname(slave_b)}",
          flush=True)
    if args.ge_p > 0:
        print(f"impair: model=GE p={args.ge_p}% r={args.ge_r}% "
              f"loss_good={args.ge_loss_good}% loss_bad={args.ge_loss_bad}%",
              flush=True)
    elif args.loss_pct > 0:
        print(f"impair: model=Bernoulli loss={args.loss_pct}%", flush=True)
    else:
        print("impair: model=passthrough (no loss)", flush=True)
    # Echo the seed so a failure can be reproduced exactly.
    if args.seed is not None:
        print(f"impair: seed={args.seed}", flush=True)
    if args.corrupt_pct > 0:
        print(f"impair: corrupt={args.corrupt_pct}%/byte", flush=True)
    if args.rate_bps > 0:
        print(f"impair: throttle={args.rate_bps}bps "
              f"({args.rate_bps // args.bits_per_byte} B/s)", flush=True)

    fwd_a = Forwarder(master_a, master_b, "a->b", args, rng)
    fwd_b = Forwarder(master_b, master_a, "b->a", args, rng)

    t1 = threading.Thread(target=fwd_a.run, daemon=True, name="a->b")
    t2 = threading.Thread(target=fwd_b.run, daemon=True, name="b->a")
    t1.start()
    t2.start()

    if args.ready_file:
        with open(args.ready_file, "w") as f:
            f.write("ready\n")

    # Clean shutdown on SIGTERM / SIGINT.
    stop = threading.Event()

    def handle_sig(signum, _frame):
        print(f"impair: caught signal {signum}, exiting", flush=True)
        stop.set()

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    try:
        while not stop.is_set():
            time.sleep(0.5)
    finally:
        # Periodic-ish stats on shutdown
        for fwd in (fwd_a, fwd_b):
            print(f"impair[{fwd.name}]: in={fwd.bytes_in}B out={fwd.bytes_out}B "
                  f"frames={fwd.frames_seen} dropped={fwd.frames_dropped}",
                  flush=True)
        for link in (args.agent_link, args.ground_link):
            try:
                os.unlink(link)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()

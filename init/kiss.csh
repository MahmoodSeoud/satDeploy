# csh init for KISS / UART transport.
#
# Used by the experiment harness to point csh at a pty endpoint that the
# impairment middleware (experiments/lib/impair.py) is forwarding to/from
# the agent's pty. Models a UHF/serial radio link.
#
# Knobs that matter for the thesis context:
#   -u <device>    pty path (the ground side; agent has its own)
#   -b <baud>      baud rate. ptys ignore termios speed, but libcsp will
#                  call cfsetospeed(); harmless. The impair.py --rate-bps
#                  flag is what actually throttles throughput.
#   -d             set as default route — without this, csp won't route
#                  toward the agent's CSP node (5425) because no rtable
#                  entry covers it.
#
# csh's CSP address (last positional) is just a node ID for the ground
# station; it does not constrain reachable peers when -d is set.
csp init
csp add kiss -u /tmp/ground_pty -b 9600 -d 19
apm load

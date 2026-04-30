# csh init for CAN transport.
#
# Used by the experiment harness with a Linux SocketCAN device — vcan0
# in dev (virtual CAN, no real bus), can0/can1 on flatsat.
#
# csh's `csp add can` syntax:
#   -c <device>    SocketCAN device name (defaults to can0)
#   -b <baud>      bitrate; libcsp calls socketcan SetBitrate. For vcan
#                  this is a no-op; for real CAN you set 1 Mbps typical.
#   -d             set as default route (see kiss.csh comment)
#
# The harness brings vcan0 up before csh starts:
#   ip link add dev vcan0 type vcan
#   ip link set up vcan0
#
# CSP runs CAN Fragmentation Protocol (CFP) automatically — fragments large
# CSP packets into 8-byte CAN frames. Frame loss is uncommon on real CAN
# (hardware ACK at controller), but bus errors and pass-window cutoffs are
# what matter; the harness's agent-kill experiments still apply on CAN.
csp init
csp add can -c vcan0 -d 19
apm load

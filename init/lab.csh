# lab.csh - the ground station for the satdeploy lab (scripts/demo.sh).
#
# Same ZMQ transport as zmq.csh, but `apm load` here picks up every APM in the
# csh plugin dir, which after scripts/demo.sh setup means three command sets at
# one prompt:
#
#   satdeploy push/status/list/rollback/logs   the deployment tool
#   csp_link test <node>                       how good is this link?
#   csp_loss start/status/stop                 packet loss, on demand
#   csp_monitor start/stop                     the wire recorder
#
# Loss and monitoring are OFF at startup: a lab that starts impaired makes the
# first thing you see a mystery. Turn them on when you want them.
#
# See docs/MANUAL.md for the walkthrough.
csp init
csp add zmq -d 19 localhost
apm load

# The lab has exactly one satellite, so make it the default target. Every
# satdeploy command falls back to this node, and `-n <addr>` overrides it when
# you genuinely have more than one.
node 5425

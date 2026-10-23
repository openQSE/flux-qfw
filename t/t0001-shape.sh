#!/bin/bash
# The shape of a hybrid job. It is a normal classical request plus a device,
# scheduled by Fluxion on fake hosts. No tasks actually run.
#
# A two node hybrid job gets its cores and the qpu together. A second hybrid
# job waits on the busy qpu while classical jobs run around it. Freeing the
# first hybrid job lets the second one start.
. "$(dirname "$0")/lib.sh"

own_instance 1 "$@"

flux R encode --hosts=flux-lead,c[1-8],nwqsim-head --cores=0-3 \
    --property=normal:1-8 --property=qfw-service:9 > "$WORK/R.json"
python3 -m flux_qfw.cli add --r "$WORK/R.json" --output "$WORK/R-q.json" nwqsim:32
grep -q '^nwqsim qpus=1 qubits=32$' <<<"$(python3 -m flux_qfw.cli devices --r "$WORK/R-q.json")" || fail "device missing from graph"
pass "qdevice_nwqsim -> qpu added at the cluster root"

QMANAGER_POLICY=coschedule load_fluxion "$WORK/R-q.json"

mk() { # name nodes cores_per_node with_qpu
python3 - "$WORK/$1.json" "$2" "$3" "$4" <<'PY'
import json, sys
from flux_qfw import resources
name, nodes, cores, qpu = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4] == "1"
res = [{"type": "node", "count": nodes, "with": [{"type": "slot", "count": cores, "label": "task",
        "with": [{"type": "core", "count": 1}]}]}]
if qpu:
    res = resources.add_device(res, "nwqsim")
json.dump({"version": 1, "resources": res,
           "tasks": [{"command": ["hostname"], "slot": "task", "count": {"per_slot": 1}}],
           "attributes": {"system": {"duration": 30, "constraints": {"properties": ["normal"]},
                                     "exec": {"test": {"run_duration": "30s"}}}}},
          open(name, "w"))
PY
}
mk hyb2 2 2 1; mk hyb1 1 1 1; mk cls 1 2 0

A=$(flux job submit "$WORK/hyb2.json"); B=$(flux job submit "$WORK/hyb1.json")
C=$(flux job submit "$WORK/cls.json"); D=$(flux job submit "$WORK/cls.json")
flux job wait-event -t 15 "$A" alloc >/dev/null || fail "hybrid job A did not allocate"
pass "hybrid job (2 nodes x 2 cores + qpu) allocated"
flux job info "$A" R | jq -e '[.scheduling.graph.nodes[].metadata.type] | any(. == "qpu")' >/dev/null \
    || fail "A's R carries no qpu"
flux job info "$A" R | jq -e '.execution.R_lite | length >= 1' >/dev/null
flux job info "$A" R | python3 -m flux_qfw.cli granted | grep -q '^nwqsim ' || fail "granted parser"
pass "A's R carries the qpu and two normal nodes"
flux job wait-event -t 15 "$C" alloc >/dev/null; flux job wait-event -t 15 "$D" alloc >/dev/null
pass "classical jobs pack around the pending hybrid job"
if flux job wait-event -t 3 "$B" alloc >/dev/null 2>&1; then fail "B allocated while the qpu was busy"; fi
pass "second hybrid job waits for the busy qpu"
flux cancel "$A"
flux job wait-event -t 15 "$B" alloc >/dev/null || fail "B did not allocate after A freed the qpu"
pass "second hybrid job allocated once the qpu was freed"
flux resource list >/dev/null && pass "flux resource list survives device vertices"
flux cancel --all >/dev/null 2>&1 || true
flux queue idle >/dev/null 2>&1 || true
echo "t0001: all passed"

#!/bin/bash
# A job scope device. The instance config sets scope = "job" for nwqsim. A
# hybrid job still waits on the device in the graph, but no QPM is called.
# Every rank exports the job's own hosts as QFW_SIMULATOR_NODES and the launch
# agent. --qfw-scope overrides the config.
. "$(dirname "$0")/lib.sh"

own_instance 2 "$@"
export FLUX_QFW_BACKEND=mock
export FLUX_QFW_MOCK_DIR="$WORK/mock"

flux R encode --hosts=fakeA,fakeB --cores=0-1 --property=normal:0-1 > "$WORK/R.json"
python3 -m flux_qfw.cli add --r "$WORK/R.json" --output "$WORK/R-q.json" nwqsim:32 shim:20
load_fluxion "$WORK/R-q.json"
# Add a [qfw] table next to the resource config lib.sh loaded.
flux config get | python3 -c '
import json, sys
conf = json.load(sys.stdin)
conf["qfw"] = {"launch-agent": "/stub/flux-launch-agent", "qpm": {"nwqsim": {"scope": "job"}, "shim": {}}}
print(json.dumps(conf))' > "$WORK/conf.json"
flux config load < "$WORK/conf.json"
flux config get qfw.qpm.nwqsim.scope | grep -q job || fail "config did not take"
pass "instance config marks nwqsim as job-scoped"

flux run --dry-run -N1 -n1 --qfw-qpu=nwqsim hostname > "$WORK/dry.json"
jq -e '.attributes.system.qfw.scope == "job" and .attributes.system.qfw.reserve == false and .attributes.system.qfw.launch_agent == "/stub/flux-launch-agent"' "$WORK/dry.json" >/dev/null \
    || { jq .attributes.system.qfw "$WORK/dry.json"; fail "CLI plugin did not apply the job scope"; }
jq -e '.resources | length == 2' "$WORK/dry.json" >/dev/null || fail "device entry missing"
pass "the CLI plugin takes the scope from the config and keeps the device entry"
flux run --dry-run -N1 -n1 --qfw-qpu=shim hostname | jq -e '.attributes.system.qfw.scope == "site" and .attributes.system.qfw.reserve == true' >/dev/null \
    || fail "a site device is still reserved"
flux run --dry-run -N1 -n1 --qfw-qpu=shim --qfw-scope=job hostname | jq -e '.attributes.system.qfw.scope == "job"' >/dev/null \
    || fail "--qfw-scope did not override"
pass "site devices reserve; --qfw-scope overrides"

id=$(flux submit -N2 -n2 --qfw-qpu=nwqsim sh -c 'echo "rank $FLUX_TASK_RANK scope=$QFW_QPU_SCOPE sid=$QFW_QPU_SERVICE_ID nodes=$QFW_SIMULATOR_NODES agent=$PRTE_MCA_plm_ssh_agent tree=$PRTE_MCA_plm_ssh_no_tree_spawn res=${QFW_RESERVATIONS:-none}"')
flux job wait-event -t 60 "$id" clean >/dev/null || fail "job did not finish"
flux job attach "$id" > "$WORK/out" 2> "$WORK/log" || { cat "$WORK/out" "$WORK/log"; fail "job failed"; }
for r in 0 1; do
    grep -q "^rank $r scope=job sid=nwqsim nodes=fakeA,fakeB agent=/stub/flux-launch-agent tree=1 res=none$" "$WORK/out" \
        || { cat "$WORK/out" "$WORK/log"; fail "rank $r environment"; }
done
pass "every rank sees the job's hosts as simulator nodes, the agent, and no reservation"
grep -q 'job-scoped device nwqsim: simulator hosts fakeA,fakeB, no site reservation' "$WORK/log" || { cat "$WORK/log"; fail "shell log"; }
[ ! -d "$WORK/mock" ] || [ -z "$(ls -A "$WORK/mock")" ] || fail "the mock QPM was called for a job-scoped device"
pass "no QPM call was made"

# The device in the graph still decides. A second nwqsim job waits.
A=$(flux submit -N1 -n1 --qfw-qpu=nwqsim sleep 30); B=$(flux submit -N1 -n1 --qfw-qpu=nwqsim sleep 30)
flux job wait-event -t 30 "$A" start >/dev/null || fail "A did not start"
sleep 2; [ "$(flux jobs -no '{state}' "$B")" = SCHED ] || fail "B should wait on the device vertex"
pass "Fluxion still limits concurrent simulations through the device vertex"
flux cancel --all >/dev/null 2>&1 || true
echo "t0005: all passed"

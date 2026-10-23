#!/bin/bash
# Several site scope jobs at once. fake-iqm has two leases and nwqsim has
# one, and six jobs are submitted together. Three run and three wait. Every
# running job gets its own reservation, and every one is unregistered.
#
# The queue policy matters. Under fcfs the third fake-iqm job, stuck on its
# device, would block the nwqsim jobs behind it. coschedule is backfill plus
# the hold, so they run.
. "$(dirname "$0")/lib.sh"

own_instance 1 "$@"
export FLUX_QFW_BACKEND=mock
export FLUX_QFW_MOCK_DIR="$WORK/mock"

# A fixed size node, so the result does not depend on how many cores this
# machine has. load_fluxion sets noverify, so the cores need not exist.
flux R encode --hosts="$(hostname)" --cores=0-7 --property=normal:0 > "$WORK/R.json"
python3 -m flux_qfw.cli add --r "$WORK/R.json" --output "$WORK/R-q.json" fake-iqm=2:20 nwqsim:32
QMANAGER_POLICY=coschedule load_fluxion "$WORK/R-q.json"
grep -q '^fake-iqm qpus=2 qubits=20$' <<<"$(python3 -m flux_qfw.cli devices)" || fail "fake-iqm has 2 qpus"

# Each job prints its reservation and keeps it for a few seconds.
job() { flux submit -N1 -n1 --qfw-qpu="$1" sh -c 'echo "$FLUX_JOB_ID $QFW_RESERVATIONS"; sleep 4'; }
ids=()
for q in fake-iqm fake-iqm fake-iqm nwqsim nwqsim nwqsim; do ids+=("$(job $q)"); done
sleep 2
running=$(flux jobs -no '{state}' "${ids[@]}" | grep -c RUN || true)
pending=$(flux jobs -no '{state}' "${ids[@]}" | grep -c SCHED || true)
[ "$running" = 3 ] && [ "$pending" = 3 ] || { flux jobs -a; fail "expected 3 running and 3 pending, got $running/$pending"; }
pass "three hybrid jobs run at once (2 fake-iqm leases + 1 nwqsim), three wait"

for id in "${ids[@]}"; do flux job wait-event -t 60 "$id" clean >/dev/null || fail "job $id did not finish"; done
pass "all six jobs finished"

for id in "${ids[@]}"; do flux job attach "$id" 2>/dev/null | grep -E '^ƒ[A-Za-z0-9]+ \[\["(fake-iqm|nwqsim)","[0-9]+"\]\]$' >> "$WORK/out" \
    || { flux job attach "$id" 2>&1 | tail -3; fail "job $id had no reservation"; }; done
n_ids=$(awk '{print $2}' "$WORK/out" | sort -u | wc -l)
[ "$n_ids" = 6 ] || { cat "$WORK/out"; fail "expected 6 distinct reservations, got $n_ids"; }
pass "every job got its own reservation id"

n_files=$(ls "$WORK/mock"/*.json | wc -l)
n_released=$(cat "$WORK/mock"/*.json | jq -s '[.[] | select(.state == "released")] | length')
[ "$n_files" = 6 ] && [ "$n_released" = 6 ] || fail "mock shows $n_files reservations, $n_released released"
pass "all six reservations were released"
echo "t0003: all passed"

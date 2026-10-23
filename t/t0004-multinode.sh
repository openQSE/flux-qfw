#!/bin/bash
# A site scope job that spans two nodes. Rank 0 registers and posts a memo,
# the other rank reads the memo after the init barrier, and every task sees
# the same QFW_RESERVATIONS. Two brokers on the local host stand in for two
# nodes with fake hostnames.
#
# This guards an old bug. Flux posts the job's start event as soon as the
# shells launch, before rank 0 has registered, so start comes before the memo
# in the eventlog. The memo reader used to stop at start, and every multi node
# job failed on rank 1.
. "$(dirname "$0")/lib.sh"

own_instance 2 "$@"
export FLUX_QFW_BACKEND=mock
export FLUX_QFW_MOCK_DIR="$WORK/mock"

flux R encode --hosts=fakeA,fakeB --cores=0-1 --property=normal:0-1 > "$WORK/R.json"
python3 -m flux_qfw.cli add --r "$WORK/R.json" --output "$WORK/R-q.json" nwqsim:32
load_fluxion "$WORK/R-q.json"
[ "$(flux resource list -s free -no '{nnodes}')" = 2 ] || fail "expected 2 free fake nodes"
pass "two-broker instance with qdevice_nwqsim"

id=$(flux submit -N2 -n2 --qfw-qpu=nwqsim sh -c 'echo "rank $FLUX_TASK_RANK $QFW_RESERVATIONS $QFW_QPU_SERVICE_ID"')
flux job wait-event -t 60 "$id" clean >/dev/null || fail "2-node hybrid job did not finish"
flux job attach "$id" > "$WORK/out" 2> "$WORK/log" || { cat "$WORK/out" "$WORK/log"; fail "2-node hybrid job failed"; }
pass "2-node hybrid job ran to completion"

grep -Eq '^rank 0 \[\["nwqsim","[0-9]+"\]\] nwqsim$' "$WORK/out" || { cat "$WORK/out"; fail "rank 0 did not see the reservation"; }
grep -Eq '^rank 1 \[\["nwqsim","[0-9]+"\]\] nwqsim$' "$WORK/out" || { cat "$WORK/out"; fail "rank 1 did not see the reservation"; }
[ "$(awk '{print $3}' "$WORK/out" | sort -u | wc -l)" = 1 ] || { cat "$WORK/out"; fail "ranks saw different reservations"; }
pass "both ranks exported the same QFW_RESERVATIONS tuple"

grep -q 'flux-shell\[1\]: flux-qfw: rank 1 exported QFW_RESERVATIONS from the job memo' "$WORK/log" \
    || { cat "$WORK/log"; fail "rank 1 did not read the memo"; }
grep -q 'flux-shell\[0\]: flux-qfw: unregistered reservation' "$WORK/log" || { cat "$WORK/log"; fail "rank 0 did not unregister"; }
pass "rank 1 read the memo after the barrier, and rank 0 unregistered at exit"

# The memo comes after start in the eventlog, and the reader still finds it.
flux job eventlog "$id" | awk '{print $2}' > "$WORK/events"
start_line=$(grep -n '^start$' "$WORK/events" | cut -d: -f1)
memo_line=$(grep -n '^memo$' "$WORK/events" | cut -d: -f1)
[ -n "$start_line" ] && [ -n "$memo_line" ] || { cat "$WORK/events"; fail "eventlog lacks start or memo"; }
[ "$memo_line" -gt "$start_line" ] && pass "memo was posted after start, and the reader still found it"

n_released=$(cat "$WORK/mock"/*.json | jq -s '[.[] | select(.state == "released")] | length')
[ "$n_released" = 1 ] || fail "mock backend shows $n_released released reservations, expected 1"
pass "exactly one reservation, released"
echo "t0004: all passed"

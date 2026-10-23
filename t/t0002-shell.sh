#!/bin/bash
# Site scope from start to finish on the local host. The CLI plugin shapes
# the jobspec and Fluxion grants the device. The shell registers the job with
# the mock QPM, the task sees QFW_RESERVATIONS, and the shell unregisters at
# exit, even when the task fails. A QPM that refuses a job Fluxion already
# granted fails it with a message that points at the QPM's admission policy.
. "$(dirname "$0")/lib.sh"

own_instance 1 "$@"
export FLUX_QFW_BACKEND=mock
export FLUX_QFW_MOCK_DIR="$WORK/mock"

# A fixed size node, so the result does not depend on how many cores this
# machine has. load_fluxion sets noverify, so the cores need not exist.
flux R encode --hosts="$(hostname)" --cores=0-7 --property=normal:0 > "$WORK/R.json"
python3 -m flux_qfw.cli add --r "$WORK/R.json" --output "$WORK/R-q.json" nwqsim:32
load_fluxion "$WORK/R-q.json"
grep -q '^nwqsim qpus=1 qubits=32$' <<<"$(python3 -m flux_qfw.cli devices)" || fail "instance graph has no device"
pass "instance loaded with a local node and qdevice_nwqsim"

if flux run --dry-run -n1 --qfw-qpu=nwqsim hostname >/dev/null 2>"$WORK/err0"; then
    fail "bare -n1 (no node level) accepted"
fi
grep -q 'add -N' "$WORK/err0" && pass "hybrid job without -N refused at submit"
flux run --dry-run -N1 -n1 --qfw-qpu=nwqsim --qfw-max-qubits=5 hostname > "$WORK/dry.json"
jq -e '.resources | length == 2 and .[1].type == "qdevice_nwqsim"' "$WORK/dry.json" >/dev/null \
    || fail "CLI plugin did not add the device entry"
jq -e '.attributes.system.qfw.max_qubits == 5 and .attributes.system.shell.options.userrc != null' \
    "$WORK/dry.json" >/dev/null || fail "envelope or userrc missing"
pass "flux run --qfw-qpu shapes the jobspec"

if flux run --dry-run -N1 -n1 --qfw-qpu=nosuch hostname >/dev/null 2>"$WORK/err"; then
    fail "unknown QPM accepted"
fi
grep -q "no QPM 'nosuch'" "$WORK/err" && pass "unknown QPM refused at submit"

# The qpu asks for the envelope's qubits, one unless the user says more, so
# Fluxion only matches a device that wide.
flux run --dry-run -N1 -n1 --qfw-qpu=nwqsim hostname \
    | jq -e '.resources[1].with[0].with == [{"type": "qubit", "count": 1}]' >/dev/null \
    || fail "the qpu does not ask for one qubit by default"
jq -e '.resources[1].with[0].with == [{"type": "qubit", "count": 5}]' "$WORK/dry.json" >/dev/null \
    || fail "the qpu does not ask for --qfw-max-qubits"
if flux run --dry-run -N1 -n1 --qfw-qpu=nwqsim --qfw-max-qubits=33 hostname >/dev/null 2>"$WORK/err1"; then
    fail "a job wider than the device was accepted"
fi
grep -q "has 32 qubits and the job asks for 33" "$WORK/err1" || { cat "$WORK/err1"; fail "qubit refusal message"; }
J=$(flux submit -N1 -n1 --qfw-qpu=nwqsim --qfw-max-qubits=32 --qfw-no-reserve true)
flux job wait-event -t 15 "$J" alloc >/dev/null || fail "a job as wide as the device did not allocate"
flux job info "$J" R | jq -e '[.scheduling.graph.nodes[].metadata.type] | any(. == "qubit")' >/dev/null \
    || fail "the job's R has no qubits"
flux job wait-event -t 15 "$J" clean >/dev/null
pass "Fluxion matches the qpu by its qubits: one by default, refused when too wide"

flux run -N1 -n1 --qfw-qpu=nwqsim --qfw-max-qubits=5 --qfw-circ-count=3 \
    sh -c 'echo "RES=$QFW_RESERVATIONS SID=$QFW_QPU_SERVICE_ID"' > "$WORK/out" 2>"$WORK/log"
grep -Eq '^RES=\[\["nwqsim","[0-9]+"\]\] SID=nwqsim$' "$WORK/out" || { cat "$WORK/out" "$WORK/log"; fail "task did not see QFW_RESERVATIONS"; }
pass "task received QFW_RESERVATIONS in QFw's tuple format"
# The shell must use the exact job id from FLUX_JOB_ID. A Lua number would
# print a large id like 1.66e+14 and break every helper call.
grep -q 'flux-qfw: job ƒ' "$WORK/log" || { cat "$WORK/log"; fail "the shell did not use the exact job id from FLUX_JOB_ID"; }
pass "the shell names the job by its exact id"
grep -q 'flux-qfw: unregistered reservation' "$WORK/log" || { cat "$WORK/log"; fail "shell did not unregister"; }
n_released=$(cat "$WORK/mock"/*.json | jq -s '[.[] | select(.state == "released")] | length')
[ "$n_released" = 1 ] || fail "mock backend shows $n_released released reservations"
pass "shell.exit unregistered the job"
jq -e '.task_class.qubit_count == 5 and .task_class.count == 3' "$WORK/mock"/*.json >/dev/null \
    && pass "envelope reached the QPM as a task_class"

# A failing task still unregisters.
flux run -N1 -n1 --qfw-qpu=nwqsim sh -c 'exit 3' >/dev/null 2>"$WORK/log2" || true
grep -q 'flux-qfw: unregistered reservation' "$WORK/log2" || { cat "$WORK/log2"; fail "no unregister after task failure"; }
pass "unregister happens even when the task fails"

# A QPM that refuses a job Fluxion granted is misconfigured. The job fails
# before the task runs, and the message says what to fix.
if FLUX_QFW_MOCK_REJECT=1 flux run -N1 -n1 --qfw-qpu=nwqsim hostname >/dev/null 2>"$WORK/log3"; then
    fail "job ran although the QPM refused it"
fi
grep -q 'admission policy to unlimited' "$WORK/log3" || { cat "$WORK/log3"; fail "refusal message does not point at the QPM's admission"; }
pass "a QPM refusal fails the job and names the misconfiguration"

# Plain classical jobs are not touched.
flux run -n1 sh -c 'test -z "${QFW_RESERVATIONS:-}"' && pass "classical job has no QFW_RESERVATIONS"
echo "t0002: all passed"

#!/bin/bash
# flux-qfw add reading a broker config and writing only R's scheduling key.
#
# The R comes from flux R parse-config on a [[resource.config]] table. The
# scheduling key it writes is loaded through [resource] scheduling, beside a
# plain R, and a hybrid job then gets its cores and the qpu together.
. "$(dirname "$0")/lib.sh"

own_instance 1 "$@"

cat > "$WORK/system.toml" <<'EOF'
[[resource.config]]
hosts = "c[1-2]"
cores = "0-3"
properties = ["normal"]
EOF

python3 -m flux_qfw.cli add --config-path "$WORK/system.toml" --output "$WORK/R-q.json" nwqsim:8
python3 - "$WORK/R-q.json" <<'PY' || fail "--config-path did not build an R with the device"
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc["execution"]["nodelist"] == ["c[1-2]"], doc["execution"]["nodelist"]
types = [n["metadata"]["type"] for n in doc["scheduling"]["graph"]["nodes"]]
assert types.count("node") == 2 and "qdevice_nwqsim" in types and "qubit" in types, types
PY
pass "--config-path builds the R from the broker config and adds the device"

python3 -m flux_qfw.cli add --config-path "$WORK/system.toml" --scheduling \
    --output "$WORK/scheduling.json" nwqsim:8
python3 - "$WORK/scheduling.json" <<'PY' || fail "--scheduling did not write only the scheduling key"
import json, sys
doc = json.load(open(sys.argv[1]))
assert set(doc) == {"graph"}, sorted(doc)
PY
grep -q '^nwqsim qpus=1 qubits=8$' <<<"$(python3 -m flux_qfw.cli devices --r "$WORK/scheduling.json")" \
    || fail "device missing from the scheduling key"
pass "--scheduling writes only the scheduling key"

flux R parse-config "$WORK/system.toml" > "$WORK/R.json"
python3 -m flux_qfw.cli add --scheduling nwqsim 2> "$WORK/err" \
    && fail "--scheduling without --r or --config-path should fail"
grep -q 'need --r or --config-path' "$WORK/err" || fail "unexpected error: $(cat "$WORK/err")"
python3 -m flux_qfw.cli add --r "$WORK/R.json" --config-path "$WORK/system.toml" nwqsim 2>/dev/null \
    && fail "--r and --config-path together should fail"
pass "--scheduling alone and --r with --config-path are refused"

# Load the plain R plus the scheduling key, as a site would with conf.d.
flux module remove -f sched-fluxion-qmanager
flux module remove -f sched-fluxion-feasibility
flux module remove -f sched-fluxion-resource
flux module remove -f sched-simple
flux module remove resource
flux config load <<EOF
[resource]
noverify = true
norestrict = true
path = "$WORK/R.json"
scheduling = "$WORK/scheduling.json"
[sched-fluxion-resource]
match-format = "rv1"
EOF
flux module load resource monitor-force-up
flux module load sched-fluxion-resource
flux module load sched-fluxion-qmanager
flux queue start --all --quiet

id=$(flux submit -N1 -n2 --qfw-qpu=nwqsim --qfw-max-qubits=8 --setattr=system.exec.test.run_duration=1s true)
flux job wait-event -t 30 "$id" alloc >/dev/null || fail "hybrid job was not allocated"
flux job info "$id" R | jq -e '[.scheduling.graph.nodes[].metadata.type] | index("qpu")' >/dev/null \
    || fail "hybrid job's R has no qpu"
pass "a hybrid job matches against the graph loaded from the scheduling key"

flux run --dry-run -N1 -n1 --qfw-qpu=nwqsim --qfw-max-qubits=9 true 2> "$WORK/err" >/dev/null \
    && fail "a job wider than the device should be refused"
grep -q 'has 8 qubits and the job asks for 9' "$WORK/err" || fail "unexpected error: $(cat "$WORK/err")"
pass "a job wider than the device is refused at submit"

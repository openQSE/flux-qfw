#!/bin/bash
# A device behind a vendor queue, set with queue = "external". One submit
# becomes two jobs. The scout job takes one core and the qpu. The user's job
# is held and asks for no device. The scout only runs while it holds a lease.
# It opens a session on the mock vendor queue, releases the user's job when
# the session is ready, and keeps the device until that job ends.
#
# Three pairs on a two lease device open only two sessions. Cancelling a held
# job leaves no session behind. A refused session cancels the held job
# instead of leaving it waiting forever.
. "$(dirname "$0")/lib.sh"

own_instance 1 "$@"

# The CLI plugin memos the scout's id onto the user's job once it submits it.
scout_of() {
    flux job eventlog --format=json "$1" | jq -r 'select(.name == "memo") | .context.qfw_scout // empty' | head -1
}
export FLUX_QFW_BACKEND=mock
export FLUX_QFW_MOCK_DIR="$WORK/mock"
export FLUX_QFW_MOCK_SESSION_DELAY=3

# A fixed size node, so the result does not depend on how many cores this
# machine has. load_fluxion sets noverify, so the cores need not exist.
flux R encode --hosts="$(hostname)" --cores=0-7 --property=normal:0 > "$WORK/R.json"
python3 -m flux_qfw.cli add --r "$WORK/R.json" --output "$WORK/R-q.json" fake-iqm=2:20 nwqsim:32
QMANAGER_POLICY=coschedule load_fluxion "$WORK/R-q.json"
flux config get | python3 -c '
import json, sys
conf = json.load(sys.stdin)
conf["qfw"] = {"qpm": {"fake-iqm": {"scope": "site", "queue": "external"}, "nwqsim": {}}}
print(json.dumps(conf))' > "$WORK/conf.json"
flux config load < "$WORK/conf.json"
flux config get qfw.qpm.fake-iqm.queue | grep -q external || fail "config did not take"
pass "instance config: fake-iqm sits behind an external queue"

flux run --dry-run -N1 -n1 --qfw-qpu=fake-iqm hostname > "$WORK/dry.json"
jq -e '.attributes.system.hold == true and .attributes.system.qfw.pair == "classical" and .attributes.system.qfw.scout == null and (.resources | length) == 1' "$WORK/dry.json" >/dev/null \
    || { jq .attributes.system "$WORK/dry.json"; fail "the classical half is not held without a device entry"; }
flux run --dry-run -N1 -n1 --qfw-qpu=nwqsim hostname | jq -e '(.attributes.system.hold // false) == false and .attributes.system.qfw.queue == "owned" and (.resources | length) == 2' >/dev/null \
    || fail "an owned device changed shape"
# The submit is expected to fail, and pipefail is on, so capture first.
out=$(flux run --dry-run -N1 -n1 --qfw-qpu=fake-iqm --qfw-scope=job hostname 2>&1 || true)
grep -q 'cannot also sit behind' <<<"$out" || { echo "$out"; fail "a job-scoped device on an external queue was accepted"; }
pass "the CLI plugin pairs exactly the jobs on an external queue"

# The held job's eight cores would fill the node, so its scout could never
# start. Seven leave a core, and so does a second node it does not ask for.
out=$(flux run --dry-run -N1 -n8 --qfw-qpu=fake-iqm hostname 2>&1 || true)
grep -q 'needs one more core for its scout' <<<"$out" || { echo "$out"; fail "a pair with no room for its scout was accepted"; }
flux run --dry-run -N1 -n7 --qfw-qpu=fake-iqm hostname >/dev/null || fail "a pair with a spare core was refused"
flux run --dry-run -N1 -n8 --qfw-qpu=nwqsim hostname >/dev/null || fail "an owned device needs no room for a scout"
pass "a pair is refused at submit when the graph has no core for its scout"

n0=$(flux jobs -a -no '{id}' | wc -l)
C=$(flux submit -N1 -n1 --qfw-qpu=fake-iqm sh -c 'echo "classical $QFW_RESERVATIONS"; sleep 3' 2> "$WORK/submit.log")
grep -q 'scout job .* submitted for fake-iqm' "$WORK/submit.log" || { cat "$WORK/submit.log"; fail "no scout was submitted"; }
S=$(scout_of "$C")
[ "$S" != null ] || fail "the classical job does not name its scout"
[ "$(flux jobs -a -no '{id}' | wc -l)" = $((n0 + 2)) ] || { flux jobs -a; fail "expected exactly two new jobs"; }
flux job info "$S" jobspec | jq -e '.attributes.system.qfw.pair == "scout" and (.resources | length) == 2 and .resources[1].type == "qdevice_fake-iqm" and .attributes.system.job.name == "scout:fake-iqm"' >/dev/null \
    || { flux job info "$S" jobspec | jq .; fail "scout jobspec"; }
pass "one submit became a pair: a scout with the qpu and a held classical job"
[ "$(flux job id --to=dec "$S")" -gt "$(flux job id --to=dec "$C")" ] || fail "the scout was submitted before the user's job"
flux job info "$S" jobspec | jq -e --argjson c "$(flux job id --to=dec "$C")" '.attributes.system.qfw.partner == $c' >/dev/null \
    || { flux job info "$S" jobspec | jq .attributes.system.qfw; fail "the scout does not name its partner"; }
pass "the user's job is submitted first and the scout names it as its partner"

flux job wait-event -t 30 "$S" start >/dev/null || fail "the scout did not start"
flux job info "$S" R | jq -e '[.scheduling.graph.nodes[].metadata.type] | any(. == "qpu")' >/dev/null || fail "the scout's R has no qpu"
sleep 1
[ "$(flux jobs -no '{state}' "$C")" = SCHED ] || { flux jobs -a; fail "the classical job should be held while the scout waits on the vendor"; }
grep -q "fake-iqm scout=.*(RUN)" <<<"$(python3 -m flux_qfw.cli sessions)" || { python3 -m flux_qfw.cli sessions; fail "sessions does not list the pair"; }
pass "the scout runs and holds the qpu; the classical job is held"

flux job wait-event -t 60 "$C" start >/dev/null || { flux job attach "$S" 2>&1 | tail -5; fail "the classical job was not released"; }
grep -q 'memo.*released=' <<<"$(flux job eventlog "$C")" || fail "no released memo on the classical job"
flux job wait-event -t 60 "$C" clean >/dev/null
flux job attach "$C" 2> "$WORK/shell.log" | grep -Eq '^classical \[\["fake-iqm","[0-9]+"\]\]$' \
    || { flux job attach "$C" 2>&1; fail "the task did not see the session reservation"; }
grep -q 'flux-qfw: job ƒ.* uses the session reservation' "$WORK/shell.log" || { cat "$WORK/shell.log"; fail "shell did not take the session by the job's exact id"; }
grep -q 'unregistered' "$WORK/shell.log" && fail "the shell must not unregister; the scout owns the session"
flux job wait-event -t 30 "$S" clean >/dev/null || fail "the scout did not exit after its partner"
grep -q 'partner finished' <<<"$(flux job attach "$S" 2>&1)" || { flux job attach "$S" 2>&1 | tail -5; fail "scout log"; }
sid=$(flux job eventlog --format=json "$C" | python3 -c 'import json,sys; ev=[json.loads(l) for l in sys.stdin]; print([e["context"]["qfw_session"] for e in ev if "qfw_session" in e.get("context", {})][0])')
[ "$(jq -r .state "$WORK/mock/$sid.json")" = released ] || fail "the session is still open after the pair ended"
pass "the scout released the partner, held the session for its lifetime, and closed it"

# Three pairs on two leases. The third scout waits on the device, so only two
# vendor sessions are ever open.
ids=(); for i in 1 2 3; do ids+=("$(flux submit -N1 -n1 --qfw-qpu=fake-iqm sleep 20 2>/dev/null)"); done
sleep 6
scouts=(); for c in "${ids[@]}"; do scouts+=("$(scout_of "$c")"); done
running=$(flux jobs -no '{state}' "${scouts[@]}" | grep -c RUN || true)
[ "$running" = 2 ] || { flux jobs -a; fail "expected two scouts running on two leases, got $running"; }
open=$(cat "$WORK"/mock/*.json | jq -s '[.[] | select(.state == "queued" or .state == "reserved")] | length')
[ "$open" = 2 ] || fail "expected two open sessions for two leases, found $open"
pass "three pairs on a two lease device: two scouts run, two sessions, the third waits on the device"
flux cancel "${ids[@]}" >/dev/null 2>&1
for s in "${scouts[@]}"; do flux job wait-event -t 30 "$s" clean >/dev/null || fail "scout $s did not exit after its partner was cancelled"; done
[ "$(cat "$WORK"/mock/*.json | jq -s '[.[] | select(.state == "queued" or .state == "reserved")] | length')" = 0 ] || fail "a session was left open"
pass "cancelling the classical jobs ends their scouts and closes the sessions"

C3=$(flux submit -N1 -n1 --qfw-qpu=fake-iqm --env=FLUX_QFW_MOCK_SESSION_REJECT=1 sleep 30 2>/dev/null)
flux job wait-event -t 30 "$C3" exception >/dev/null || fail "no exception raised on the partner of a refused session"
flux job wait-event -t 10 "$C3" clean >/dev/null || true
pass "a refused session cancels the held partner with an exception"

# The scout's clock starts before its partner's, so its limit is the session
# wait plus the partner's duration plus a margin, not the partner's duration.
C4=$(flux submit -N1 -n1 -t 2m --qfw-qpu=fake-iqm --qfw-session-wait=10m sleep 1 2>/dev/null)
S4=$(scout_of "$C4")
flux job info "$S4" jobspec | jq -e '.attributes.system.duration == 780 and .attributes.system.qfw.session_wait == 600' >/dev/null \
    || { flux job info "$S4" jobspec | jq .attributes.system; fail "the scout's limit is not the wait plus the partner's duration"; }
flux job wait-event -t 60 "$C4" clean >/dev/null || fail "the partner with a session wait did not finish"
pass "the scout's time limit covers the session wait and the partner's duration"

# A vendor slower than the session wait fails the partner instead of holding
# its nodes, and the scout closes the session it opened.
C5=$(flux submit -N1 -n1 --qfw-qpu=fake-iqm --qfw-session-wait=2s --env=FLUX_QFW_MOCK_SESSION_DELAY=30 sleep 30 2>/dev/null)
S5=$(scout_of "$C5")
flux job wait-event -t 30 "$C5" exception >/dev/null || fail "no exception raised when the session wait ran out"
flux job wait-event -t 30 "$S5" clean >/dev/null || fail "the scout did not exit after the session wait"
# The scout exits 1 here, and pipefail is on, so capture first.
out=$(flux job attach "$S5" 2>&1 || true)
grep -q 'not ready within the session wait' <<<"$out" || { tail -5 <<<"$out"; fail "scout log"; }
[ "$(cat "$WORK"/mock/*.json | jq -s '[.[] | select(.state == "queued" or .state == "reserved")] | length')" = 0 ] || fail "a session was left open after the wait ran out"
pass "a session that is not ready within the wait fails the partner and is closed"
flux cancel --all >/dev/null 2>&1 || true
echo "t0006: all passed"

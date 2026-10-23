"""The scout job, which holds a vendor device for the user's job.

A device behind a vendor queue is not ready just because Fluxion granted it.
So flux submit --qfw-qpu on such a device submits two jobs.

The user's job asks for its normal nodes and no device. It is submitted
first, and held. Under the coschedule policy Fluxion keeps its nodes
reserved but does not start it.

The scout job is submitted next, with the user's job id in its qfw.partner.
It asks for one core and the exclusive qpu. It waits in the queue like any
job, so it only runs while it holds a lease. That means the number of open
vendor sessions is never more than the number of leases. This module is the
scout job's program.

When the scout starts, running as the user, it does these things in order.

1. Read its partner from qfw.partner. If the partner already ended, exit.
2. Open a session on the vendor queue and memo qfw_session onto the partner.
3. Wait for the session. If the partner is cancelled meanwhile, close the
   session and exit. If the session is not ready within the session wait,
   counted from when the scout started, close it and fail the partner. The
   scout's time limit is that wait plus the partner's duration, so a scout
   that gets this far outlives the partner.
4. When the session is ready, memo qfw_reservations and released onto the
   partner, then release its hold. The partner's shell exports the memo as
   QFW_RESERVATIONS. The released memo stops the qmanager from holding the
   job again after a reload.
5. Keep the device and the session until the partner ends, then close the
   session and exit.

If the vendor refuses the session, the partner gets a qfw-session exception.
If the scout is cancelled, it closes the session and the partner gets a
qfw-scout exception. Either way a held job is never left waiting forever.
"""

import json
import os
import pwd
import signal
import subprocess
import sys
import time

import flux
import flux.constants
import flux.job
import flux.job.info
import flux.job.kvslookup

from .envelope import Envelope
from .qpm import JobIdentity, SessionError, get_backend
from .qpm.base import encode_reservations

RELEASE_TOPIC = "sched-fluxion-qmanager.release"
SESSION_WAIT_DEFAULT = 3600.0


def log(fmt, *args):
    print("flux-qfw scout: " + fmt.format(*args), file=sys.stderr, flush=True)


def job_memo(jobid, **memo):
    """Post a memo onto a job. It goes in the eventlog, so it survives a restart."""
    argv = ["flux", "job", "memo", str(jobid)]
    for key, value in memo.items():
        argv.append(
            "{}={}".format(key, value if isinstance(value, str) else json.dumps(value))
        )
    subprocess.run(argv, check=True, stdout=subprocess.DEVNULL)


def job_raise(jobid, message, exc_type="qfw-session"):
    subprocess.run(
        [
            "flux",
            "job",
            "raise",
            "--type",
            exc_type,
            "--severity",
            "0",
            str(jobid),
            message,
        ],
        check=False,
    )


def active_jobs(handle, userid=None):
    """Return the pending and running jobs of every user, or of one user.

    This calls the job-list RPC directly. flux.job.job_list() fails when
    nothing matches. The RPC's own state filter is not strict, so the state
    is checked here too. Running jobs are included because, when the hold
    has no effect, the partner is already running by the time the scout
    looks.
    """
    payload = {
        "max_entries": 1000,
        "attrs": ["userid", "state"],
        "userid": flux.constants.FLUX_USERID_UNKNOWN if userid is None else int(userid),
        "states": flux.constants.FLUX_JOB_STATE_ACTIVE,
    }
    jobs = handle.rpc("job-list.list", payload).get()["jobs"]
    return [j for j in jobs if j.get("state", 0) & flux.constants.FLUX_JOB_STATE_ACTIVE]


def job_state(handle, jobid):
    """Return the job state the way flux jobs prints it, like RUN or SCHED."""
    info = flux.job.job_list_id(handle, int(jobid), attrs=["state"]).get()["job"]
    return flux.job.info.statetostr(info["state"])


def job_system(handle, jobid):
    """Return attributes.system from a job's jobspec."""
    spec = flux.job.kvslookup.job_kvs_lookup(handle, int(jobid), keys=["jobspec"])[
        "jobspec"
    ]
    return (spec.get("attributes") or {}).get("system") or {}


def release_hold(handle, jobid):
    handle.rpc(RELEASE_TOPIC, {"id": int(jobid)}).get()


def scout(partner=None, poll_s=2.0, handle=None):
    """Run the scout for the job this process belongs to. Returns the exit code."""
    h = handle or flux.Flux()
    started = time.time()
    my_id = os.environ.get("FLUX_JOB_ID")
    if not my_id:
        log("FLUX_JOB_ID is not set; the scout only runs as the program of a scout job")
        return 2
    my_id = int(flux.job.JobID(my_id))
    userid = os.getuid()
    me = job_system(h, my_id).get("qfw") or {}
    envelope = Envelope.from_attr(me) if me.get("qpu") else None
    if envelope is None:
        log("job {} has no qfw envelope; was it submitted with --qfw-qpu?", my_id)
        return 2
    service_id = envelope.qpu
    backend = get_backend(me.get("backend"))
    deadline = started + float(me.get("session_wait") or SESSION_WAIT_DEFAULT)

    partner = partner or me.get("partner")
    if partner is None:
        log("job {} names no partner; was it submitted by the flux-qfw plugin?", my_id)
        return 2
    partner = int(flux.job.JobID(partner))
    # A scout can wait in the queue for a lease while its partner is
    # cancelled. Then it should give the lease back right away.
    if job_state(h, partner) == "INACTIVE":
        log("job {} already ended, so scout {} has nothing to do", partner, my_id)
        return 0
    log("scout {} holds {} for job {}", my_id, service_id, partner)
    try:
        username = pwd.getpwuid(userid).pw_name
    except KeyError:
        username = str(userid)
    identity = JobIdentity(
        jobid=partner,
        userid=userid,
        username=username,
        cluster=os.environ.get("FLUX_QFW_CLUSTER", "flux"),
    )
    duration = int(job_system(h, partner).get("duration") or 0)

    session = None

    def close(reason):
        if session is not None:
            try:
                backend.close_session(service_id, session.session_id)
                log("session {} closed ({})", session.session_id, reason)
            except Exception as error:
                log("closing session {}: {}", session.session_id, error)

    def on_term(signum, frame):
        close("scout terminated")
        if job_state(h, partner) != "INACTIVE":
            job_raise(
                partner,
                "flux-qfw: the scout for {} was terminated".format(service_id),
                "qfw-scout",
            )
        sys.exit(143)

    signal.signal(signal.SIGTERM, on_term)

    try:
        session = backend.open_session(identity, service_id, envelope, duration)
    except SessionError as error:
        log("the queue for {} refused a session: {}", service_id, error)
        job_raise(
            partner, "flux-qfw: {} refused a session: {}".format(service_id, error)
        )
        return 1
    job_memo(
        partner,
        qfw_session=session.session_id,
        qfw_session_state=session.state,
    )
    log("session {} opened on {} ({})", session.session_id, service_id, session.state)

    while not session.ready:
        if job_state(h, partner) == "INACTIVE":
            close("partner ended while queued")
            return 0
        if time.time() >= deadline:
            close("not ready within the session wait")
            job_raise(
                partner,
                "flux-qfw: the session on {} was not ready within {:.0f}s".format(
                    service_id, deadline - started
                ),
            )
            return 1
        time.sleep(poll_s)
        session = backend.session_status(service_id, session.session_id)
        if session.state == "failed":
            log("session {} failed: {}", session.session_id, session.detail)
            job_raise(
                partner,
                "flux-qfw: the session on {} failed: {}".format(
                    service_id, session.detail
                ),
            )
            return 1

    reservation = session.reservation()
    tuple_value = encode_reservations([reservation])
    # Post the memos before the release. The partner's shell reads them when
    # it starts, so they have to be there first.
    job_memo(
        partner,
        qfw_reservations=tuple_value,
        qfw_session_state="ready",
        released=session.session_id,
    )
    try:
        release_hold(h, partner)
        log(
            "session {} is ready, job {} released, QFW_RESERVATIONS={}",
            session.session_id,
            partner,
            tuple_value,
        )
    except Exception as error:
        # The job was not held, which happens under other queue policies, or
        # it is already gone. The memo is posted, so a running shell can use it.
        log("could not release job {} ({}), but the memo is posted", partner, error)

    # The device and the session belong to the pair until the partner ends.
    while job_state(h, partner) != "INACTIVE":
        time.sleep(poll_s)
    close("partner finished")
    return 0


def sessions(handle=None):
    """Return the scout pairs in flight.

    Each row is (job id, device, scout id, scout state, session id, state),
    for every user's job that is still pending or running. The scout id
    comes from the qfw_scout memo the CLI plugin posts on the user's job.
    """
    h = handle or flux.Flux()
    out = []
    for job in active_jobs(h):
        jobid = int(job["id"])
        try:
            data = flux.job.kvslookup.job_kvs_lookup(
                h, jobid, keys=["jobspec", "eventlog"]
            )
        except Exception:
            continue
        system = ((data.get("jobspec") or {}).get("attributes") or {}).get(
            "system"
        ) or {}
        qfw = system.get("qfw") or {}
        if (
            not system.get("hold")
            or not qfw.get("hold")
            or qfw.get("pair") != "classical"
        ):
            continue
        scout_id, session_id, state = None, None, "waiting for the scout"
        eventlog = data.get("eventlog") or ""
        lines = eventlog.splitlines() if isinstance(eventlog, str) else eventlog
        for line in lines:
            try:
                event = json.loads(line) if isinstance(line, str) else line
            except ValueError:
                continue
            if event.get("name") != "memo":
                continue
            ctx = event.get("context") or {}
            if "qfw_scout" in ctx:
                scout_id = int(ctx["qfw_scout"])
            if "qfw_session" in ctx:
                session_id = str(ctx["qfw_session"])
                state = "session {}".format(ctx.get("qfw_session_state", "queued"))
            if "released" in ctx:
                state = "released, waiting for nodes"
        try:
            scout_state = job_state(h, scout_id) if scout_id else "none"
        except Exception:
            scout_state = "unknown"
        if job_state(h, jobid) == "RUN":
            state = "running with the session"
        out.append((jobid, qfw.get("qpu"), scout_id, scout_state, session_id, state))
    return out

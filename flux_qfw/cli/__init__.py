"""The flux-qfw helper, also run as python -m flux_qfw.cli.

The shell plugin and the scout job call it, and operators use it to look at
and change the graph.

  register    tell a site QPM that a job holds its device, and print the
              QFW_RESERVATIONS value
  unregister  tell a site QPM that the job is done
  granted     list the devices in a job's R
  devices     list the devices in the instance graph
  add         add devices to a running instance, or with --r to an R file
  memo        print the QFW_RESERVATIONS value memoed onto a job
  scout       the program a scout job runs for a device behind a vendor queue
  sessions    list the scout pairs in flight and where each one stands
  status      ask a backend about a QPM
  plugins     print the directory to put on FLUX_CLI_PLUGINPATH
"""

import argparse
import json
import os
import sys
import time

import flux
import flux.job
import flux.job.kvslookup

from .. import instance, resources, scout
from ..envelope import Envelope
from ..qpm import ReservationError, SessionError, get_backend
from ..qpm.base import JobIdentity, encode_reservations


def _read_json(path):
    if path in (None, "-"):
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def cmd_register(args):
    """Tell the site QPM that this job holds its device.

    Fluxion has already given the job the device. The QPM only needs to know,
    so it can hand back the reservation id QFw expects. A refusal here means
    the QPM is doing its own admission on top of Fluxion's, which is a site
    configuration problem.
    """
    envelope = Envelope.from_attr(json.loads(args.envelope))
    job = JobIdentity(
        jobid=int(flux.job.JobID(args.jobid)),
        userid=int(args.userid),
        username=args.username,
        cluster=args.cluster,
        gid=int(args.gid),
    )
    backend = get_backend(args.backend)
    try:
        reservation = backend.reserve(job, envelope.qpu, envelope, args.duration)
    except ReservationError as error:
        raise ReservationError(
            "QPM {} refused job {} although Fluxion granted it the device ({}). "
            "Fluxion decides who holds the device, so set the QPM's admission "
            "policy to unlimited".format(envelope.qpu, args.jobid, error)
        ) from error
    # The shell plugin keeps only the last line of output.
    print(encode_reservations([reservation]))


def cmd_unregister(args):
    backend = get_backend(args.backend)
    result = backend.release(args.service, args.reservation_id, args.reason)
    print(json.dumps(result))


def cmd_granted(args):
    for sid, paths in resources.granted(_read_json(args.r)):
        print("{} {}".format(sid, ",".join(p for p in paths if p)))


def cmd_devices(args):
    doc = _read_json(args.r) if args.r else instance.load_instance_graph()
    qubits = resources.device_qubits(doc)
    for sid, count in sorted(resources.devices_in(doc).items()):
        print("{} qpus={} qubits={}".format(sid, count, qubits.get(sid) or "none"))


def cmd_memo(args):
    """Print the QFW_RESERVATIONS value memoed onto a job.

    The shell plugin calls this on every rank of the user's half of a scout
    pair, and on ranks other than 0 of a site scope job. It polls the
    eventlog until the memo shows up. A blocking watch cannot honor a
    timeout, and a job whose memo never comes has to fail instead of hang.
    """
    h = flux.Flux()
    jobid = int(flux.job.JobID(args.jobid))
    deadline = time.time() + args.timeout
    while True:
        try:
            eventlog = flux.job.kvslookup.job_kvs_lookup(h, jobid, keys=["eventlog"])[
                "eventlog"
            ]
        except Exception:
            eventlog = ""
        lines = eventlog.splitlines() if isinstance(eventlog, str) else (eventlog or [])
        terminal = False
        for line in lines:
            try:
                event = json.loads(line) if isinstance(line, str) else line
            except ValueError:
                continue
            if event.get("name") == "memo" and "qfw_reservations" in (
                event.get("context") or {}
            ):
                value = event["context"]["qfw_reservations"]
                print(
                    value
                    if isinstance(value, str)
                    else json.dumps(value, separators=(",", ":"))
                )
                return
            if event.get("name") in ("exception", "finish", "clean"):
                terminal = True
        if terminal:
            raise ValueError(
                "job {} ended without a qfw_reservations memo".format(args.jobid)
            )
        if time.time() >= deadline:
            raise ValueError(
                "job {} posted no qfw_reservations memo within {}s".format(
                    args.jobid, args.timeout
                )
            )
        time.sleep(0.5)


def cmd_add(args):
    """Add devices to a running instance, or to an R from a file or broker config."""
    specs = [resources.parse_spec(item) for item in args.qpu]
    if args.r is None and args.config_path is None:
        if args.output or args.scheduling:
            raise ValueError("--output and --scheduling need --r or --config-path")
        added = instance.add_devices(specs)
        print("added: {}".format(", ".join(added) or "nothing (already present)"))
        return
    if args.config_path is not None:
        doc = instance.encode_r(instance.r_from_config(args.config_path))
    else:
        doc = instance.encode_r(_read_json(args.r))
    added = resources.add_devices_to_graph(doc, specs)
    if args.scheduling:
        doc = doc.get("scheduling", doc)
    if args.output:
        with open(args.output, "w") as fh:
            json.dump(doc, fh)
    else:
        json.dump(doc, sys.stdout)
        sys.stdout.write("\n")
    print(
        "added: {}".format(", ".join(added) or "nothing (already present)"),
        file=sys.stderr,
    )


def cmd_scout(args):
    return scout.scout(partner=args.partner, poll_s=args.poll)


def cmd_sessions(args):
    rows = scout.sessions()
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "id": j,
                        "qpu": q,
                        "scout": sc,
                        "scout_state": scs,
                        "session": s,
                        "state": st,
                    }
                    for j, q, sc, scs, s, st in rows
                ]
            )
        )
        return
    if not rows:
        print("no external-queue pairs in flight")
        return
    for jobid, qpu, scout_id, scout_state, session_id, state in rows:
        print(
            "{} {} scout={} ({}) session={} {}".format(
                flux.job.JobID(jobid).f58,
                qpu,
                flux.job.JobID(scout_id).f58 if scout_id else "-",
                scout_state,
                session_id or "-",
                state,
            )
        )


def cmd_status(args):
    print(json.dumps(get_backend(args.backend).status(args.service), default=str))


def cmd_plugins(args):
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print(os.path.join(here, "cli-plugins"))


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="flux-qfw",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("register")
    r.add_argument("--jobid", required=True)
    r.add_argument("--userid", required=True)
    r.add_argument("--username", required=True)
    r.add_argument("--gid", default=0)
    r.add_argument("--cluster", default=os.environ.get("FLUX_QFW_CLUSTER", "flux"))
    r.add_argument("--envelope", required=True, help="attributes.system.qfw as JSON")
    r.add_argument("--duration", type=int, default=0, help="job duration in seconds")
    r.add_argument("--backend")
    r.set_defaults(func=cmd_register)

    u = sub.add_parser("unregister")
    u.add_argument("--service", required=True)
    u.add_argument("--reservation-id", required=True)
    u.add_argument("--reason", type=int, default=1)
    u.add_argument("--backend")
    u.set_defaults(func=cmd_unregister)

    g = sub.add_parser("granted")
    g.add_argument("--r", default="-", help="R file, or - for stdin")
    g.set_defaults(func=cmd_granted)

    d = sub.add_parser("devices")
    d.add_argument("--r", help="R or graph file, default the running instance")
    d.set_defaults(func=cmd_devices)

    m = sub.add_parser("memo")
    m.add_argument("--jobid", required=True)
    m.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="seconds to wait for the memo, default 60",
    )
    m.set_defaults(func=cmd_memo)

    a = sub.add_parser("add")
    source = a.add_mutually_exclusive_group()
    source.add_argument(
        "--r",
        help="add to this R or graph file instead of the running instance, - for stdin",
    )
    source.add_argument(
        "--config-path",
        help="add to the R that flux R parse-config builds from this broker config",
    )
    a.add_argument(
        "--output", help="with --r or --config-path, write the result here, not stdout"
    )
    a.add_argument(
        "--scheduling",
        action="store_true",
        help="write only R's scheduling key, for [resource] scheduling",
    )
    a.add_argument(
        "qpu",
        nargs="+",
        help="SERVICE_ID[=QPUS][:QUBITS], like fake-iqm=2:20; both default to 1",
    )
    a.set_defaults(func=cmd_add)

    sc = sub.add_parser("scout")
    sc.add_argument(
        "--partner",
        help="the held job to serve, default the qfw.partner in this job's jobspec",
    )
    sc.add_argument(
        "--poll", type=float, default=2.0, help="seconds between checks, default 2"
    )
    sc.set_defaults(func=cmd_scout)

    se = sub.add_parser("sessions")
    se.add_argument("--json", action="store_true")
    se.set_defaults(func=cmd_sessions)

    s = sub.add_parser("status")
    s.add_argument("--service", required=True)
    s.add_argument("--backend")
    s.set_defaults(func=cmd_status)

    sub.add_parser("plugins").set_defaults(func=cmd_plugins)

    args = p.parse_args(argv)
    try:
        rc = args.func(args)
    except (ReservationError, SessionError, ValueError) as error:
        sys.exit("flux-qfw: {}".format(error))
    if rc:
        sys.exit(int(rc))


if __name__ == "__main__":
    main()

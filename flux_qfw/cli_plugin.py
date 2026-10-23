"""The flux CLI plugin that adds the --qfw-* options.

    flux run -N1 -n4 --qfw-qpu=nwqsim --qfw-max-qubits=5 ./qfw_qiskit_simple.sh 5

It runs in the user's process when they submit, and only shapes the jobspec.

For a device the site owns, the classical request stays as flux built it and
a second entry asks for the exclusive qpu, with as many qubits as the
envelope's max_qubits. Fluxion gives the job both at once, or the job waits
while the device is busy, just like a busy core.

For a device behind a vendor queue, one submit becomes two jobs. The user's
job is submitted first, held, and asks for no device. Then a scout job is
submitted with the user's job id as its partner. It asks for one core and
the qpu, and releases the user's job once the vendor is ready. See scout.py.

The envelope goes into attributes.system.qfw for the shell plugin, and the
shell plugin is added through the userrc shell option, so the site does not
need any shell configuration.
"""

import copy
import json
import os
import sys

import flux
import flux.job
import flux.util
from flux.cli.plugin import CLIPlugin
from flux.job import JobspecV1

from . import instance, resources
from .envelope import WORKLOAD_KINDS, Envelope

USERRC = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "flux_plugins", "flux-qfw.lua"
)
_ACTIVE_PROGS = ("submit", "run", "batch", "bulksubmit", "alloc")

# How long a scout waits for the vendor before it gives up, unless the device
# or the job says otherwise.
SESSION_WAIT_DEFAULT = 3600.0

# Slack between the scout's release of the user's job and that job starting.
# The scout's time limit covers the wait, the user's duration, and this.
SCOUT_MARGIN = 60.0


def envelope_from_args(args):
    """Return the envelope for these options, or None when no device was asked for."""
    qpu = getattr(args, "qpu", None)
    if not qpu:
        for name in ("max_qubits", "circ_count", "max_depth", "max_shots"):
            if getattr(args, name, None):
                raise ValueError(
                    "--qfw-{} needs --qfw-qpu".format(name.replace("_", "-"))
                )
        return None
    kwargs = {"qpu": qpu}
    for name in (
        "workload_kind",
        "circ_count",
        "max_qubits",
        "max_depth",
        "max_shots",
        "one_q_gates",
        "two_q_gates",
        "measurements",
        "qpus",
    ):
        value = getattr(args, name, None)
        if value is not None:
            kwargs["circuit_count" if name == "circ_count" else name] = value
    return Envelope(**kwargs)


def device_config(service_id):
    """Return this device's table from the instance's [qfw] config.

    Returns an empty dict outside an instance, or when there is no table.
    """
    try:
        conf = flux.Flux().conf_get("qfw", None) or {}
    except Exception:
        return {}
    spec = dict((conf.get("qpm") or {}).get(service_id) or {})
    if "launch-agent" in conf and "launch-agent" not in spec:
        spec["launch-agent"] = conf["launch-agent"]
    return spec


def instance_graph_or_none():
    """Return the instance graph, or None when it cannot be read.

    That happens with flux run --dry-run outside an instance.
    """
    try:
        return instance.load_instance_graph()
    except Exception:
        return None


def check_device_known(service_id, doc, qubits=1):
    """Refuse the job now when the graph has no such device, or it is too narrow.

    Fluxion would refuse it later with a bare "type must be known" or
    "unsatisfiable". This names the devices the graph does have, and how
    many qubits the device has.
    """
    if doc is None:
        return
    known = resources.devices_in(doc)
    if service_id not in known:
        raise ValueError(
            "no QPM {!r} in the resource graph; known: {}".format(
                service_id, ", ".join(sorted(known)) or "none"
            )
        )
    width = resources.device_qubits(doc).get(service_id)
    if width is None:
        raise ValueError(
            "QPM {!r} has no qubits in the resource graph; add it again with "
            "flux-qfw add {}=QPUS:QUBITS".format(service_id, service_id)
        )
    if qubits > width:
        raise ValueError(
            "QPM {!r} has {} qubits and the job asks for {}".format(
                service_id, width, qubits
            )
        )


class QfwCLIPlugin(CLIPlugin):
    """Ask for a QFw device along with the classical resources."""

    def __init__(self, prog):
        super().__init__(prog, prefix="qfw")
        if self.prog not in _ACTIVE_PROGS:
            return
        self.add_option(
            "--qpu",
            metavar="SERVICE_ID",
            help="the QFw device to use, for example nwqsim or iqm-ornl-20q",
        )
        self.add_option(
            "--qpus",
            metavar="N",
            type=int,
            default=1,
            help="how many leases to take on the device, default 1",
        )
        self.add_option(
            "--workload-kind",
            metavar="KIND",
            choices=WORKLOAD_KINDS,
            default="hybrid",
            help="quantum or hybrid, default hybrid",
        )
        self.add_option(
            "--circ-count", metavar="N", type=int, help="circuits the job will submit"
        )
        self.add_option(
            "--max-qubits", metavar="N", type=int, help="largest circuit width"
        )
        self.add_option(
            "--max-depth", metavar="N", type=int, help="largest circuit depth"
        )
        self.add_option(
            "--max-shots", metavar="N", type=int, help="most shots per circuit"
        )
        self.add_option(
            "--one-q-gates", metavar="N", type=int, help="most one qubit gates"
        )
        self.add_option(
            "--two-q-gates", metavar="N", type=int, help="most two qubit gates"
        )
        self.add_option(
            "--measurements", metavar="N", type=int, help="most measurements"
        )
        self.add_option(
            "--backend",
            metavar="NAME",
            help="the QPM backend, defw (default) or mock",
        )
        self.add_option(
            "--no-reserve",
            action="store_true",
            help="schedule the device but do not tell the QPM",
        )
        self.add_option(
            "--scope",
            metavar="SCOPE",
            choices=("site", "job"),
            help="site means the site runs the QPM and the job registers with it. "
            "job means the job runs the simulator on its own nodes. "
            "Defaults to the device's [qfw] config",
        )
        self.add_option(
            "--queue",
            metavar="QUEUE",
            choices=("owned", "external"),
            help="owned means the site controls the device, so nodes and device "
            "are given together. external means the device is behind a vendor "
            "queue, so a scout job holds it and this job waits until the vendor "
            "is ready. Defaults to the device's [qfw] config",
        )
        self.add_option(
            "--session-wait",
            metavar="FSD",
            help="how long the scout waits for a vendor session before the job "
            "fails, like 30m or 2h. Defaults to the device's session-wait, "
            "or 1h",
        )

    def modify_jobspec(self, args, jobspec):
        try:
            envelope = envelope_from_args(args)
        except ValueError as error:
            raise SystemExit("flux-qfw: {}".format(error))
        if envelope is None:
            return
        raw = jobspec.jobspec
        doc = instance_graph_or_none()
        try:
            check_device_known(envelope.qpu, doc, envelope.max_qubits)
            classical = raw["resources"]
            if doc is not None:
                # A graph with sockets needs every level named, or the flat
                # request flux builds will not match it.
                classical = resources.mirror_path(
                    classical, resources.node_to_core_path(doc)
                )
            raw["resources"] = resources.add_device(
                classical, envelope.qpu, envelope.qpus, envelope.max_qubits
            )
        except ValueError as error:
            raise SystemExit("flux-qfw: {}".format(error))
        attr = envelope.to_attr()

        # Scope says who runs the QPM. With site scope the site runs it and
        # the job shell registers the job with it. With job scope the job runs
        # the simulator itself on its own nodes, so there is nothing to
        # register and the shell exports the job's hosts instead.
        spec = device_config(envelope.qpu)
        scope = getattr(args, "scope", None) or spec.get("scope") or "site"
        if scope not in ("site", "job"):
            raise SystemExit(
                "flux-qfw: scope must be site or job, not {!r}".format(scope)
            )
        attr["scope"] = scope
        attr["reserve"] = scope == "site" and not getattr(args, "no_reserve", False)

        # Queue says who controls the device. An owned device is ready the
        # moment Fluxion grants it. An external device is behind a vendor
        # queue, so this submit becomes a scout job that holds the device and
        # this job, held, with no device of its own.
        queue = getattr(args, "queue", None) or spec.get("queue") or "owned"
        if queue not in ("owned", "external"):
            raise SystemExit(
                "flux-qfw: queue must be owned or external, not {!r}".format(queue)
            )
        if queue == "external" and scope == "job":
            raise SystemExit(
                "flux-qfw: {} is job-scoped (the job runs it itself) and cannot also sit "
                'behind an external queue; give it scope = "site" or --qfw-scope=site'.format(
                    envelope.qpu
                )
            )
        attr["queue"] = queue
        if queue == "external":
            try:
                attr["session_wait"] = session_wait(args, spec)
            except ValueError as error:
                raise SystemExit("flux-qfw: {}".format(error))
        if spec.get("launch-agent"):
            attr["launch_agent"] = spec["launch-agent"]
        attr["backend"] = (
            getattr(args, "backend", None)
            or os.environ.get("FLUX_QFW_BACKEND")
            or "defw"
        )

        # The shell runs the helper with the same python and PYTHONPATH used
        # here, so it finds the same flux_qfw.
        attr["python"] = sys.executable
        attr["pythonpath"] = os.environ.get("PYTHONPATH", "")
        attr["hold"] = queue == "external" and attr["reserve"]
        if attr["hold"]:
            # The held job's nodes are reserved, so its scout needs a core
            # outside them, or it never starts and the job waits forever.
            system = raw.get("attributes", {}).get("system", {}) or {}
            reason = (
                resources.fits_with_scout(doc, classical, system.get("constraints"))
                if doc is not None
                else None
            )
            if reason:
                raise SystemExit(
                    "flux-qfw: {} is behind a vendor queue and needs one more core "
                    "for its scout, but {}".format(envelope.qpu, reason)
                )
            raw["resources"] = classical
            jobspec.setattr("system.hold", True)
            attr["pair"] = "classical"
            install_scout_submitter()
        jobspec.setattr("system.qfw", attr)
        jobspec.setattr_shell_option(
            "userrc", os.environ.get("FLUX_QFW_USERRC", USERRC)
        )


def session_wait(args, spec):
    """Return how many seconds the scout waits for the vendor.

    The option wins over the device's session-wait. Both take a Flux
    duration like 30m, or a number of seconds.
    """
    value = getattr(args, "session_wait", None) or spec.get("session-wait")
    if value is None:
        return SESSION_WAIT_DEFAULT
    seconds = flux.util.parse_fsd(str(value))
    if seconds <= 0:
        raise ValueError("session wait must be more than zero, not {!r}".format(value))
    return seconds


_submit_async = None


def install_scout_submitter():
    """Submit the scout right after the user's job, once its id is known.

    The flux CLI has no hook that runs after submit, but every submit, run,
    batch, alloc and bulksubmit goes through flux.job.submit_async. So this
    wraps it for this process only. A held classical jobspec is submitted as
    usual, and as soon as its id comes back the scout is submitted with that
    id as its partner. Any other jobspec passes through untouched.
    """
    global _submit_async
    if _submit_async is not None:
        return
    _submit_async = flux.job.submit_async

    def submit_async(flux_handle, jobspec, *args, **kwargs):
        future = _submit_async(flux_handle, jobspec, *args, **kwargs)
        spec = held_classical(jobspec)
        if spec is None:
            return future
        return PairFuture(future, flux_handle, spec)

    flux.job.submit_async = submit_async


class PairFuture:
    """The user's submit future, which submits the scout when it resolves.

    It cannot wait on the future itself. A flux future that was read
    synchronously never runs a callback added later, and flux submit adds
    one. So the scout goes in from that callback, or from get_id for the
    commands that wait synchronously, whichever comes first.
    """

    def __init__(self, future, handle, spec):
        self._future = future
        self._handle = handle
        self._spec = spec
        self._paired = False

    def _pair(self):
        if self._paired:
            return
        self._paired = True
        try:
            jobid = self._future.get_id()
        except OSError:
            # The CLI reports the failed submit, and there is no partner.
            return
        submit_scout(self._handle, self._spec, jobid)

    def get_id(self):
        self._pair()
        return self._future.get_id()

    def then(self, callback, *args, **kwargs):
        def paired(_future):
            self._pair()
            callback(self, *args, **kwargs)

        self._future.then(paired)
        return self

    def __getattr__(self, name):
        return getattr(self._future, name)


def held_classical(jobspec):
    """Return the jobspec as a dict if it is the held half of a scout pair."""
    try:
        if hasattr(jobspec, "jobspec"):
            spec = jobspec.jobspec
        elif isinstance(jobspec, dict):
            spec = jobspec
        else:
            spec = json.loads(jobspec)
    except (TypeError, ValueError):
        return None
    system = (spec.get("attributes") or {}).get("system") or {}
    qfw = system.get("qfw") or {}
    if system.get("hold") and qfw.get("pair") == "classical":
        return spec
    return None


def submit_scout(handle, spec, partner):
    """Submit the scout job for the held job partner and return its id.

    The scout asks for one core and the exclusive qpu, so it waits in the
    queue like any job and only runs while it holds a lease. It has the same
    envelope and backend as the user's job, and partner in its qfw.partner,
    so it knows whom it serves. The scout's id is memoed onto the partner as
    qfw_scout.

    The scout's clock starts before the user's job does, because it covers
    the wait for the vendor too. So its time limit is the session wait plus
    the user's duration, or the scout would end, and take the session with
    it, while the user's job still runs. With no duration it has no limit.

    If the scout cannot be submitted the partner is cancelled, since nothing
    would ever release it.
    """
    system = spec["attributes"]["system"]
    attr = system["qfw"]
    envelope = Envelope.from_attr(attr)
    scout = JobspecV1.from_command(
        [attr["python"], "-m", "flux_qfw.cli", "scout"],
        num_tasks=1,
        num_nodes=1,
        cores_per_task=1,
    )
    scout.setattr("system.job.name", "scout:{}".format(envelope.qpu))
    if system.get("duration"):
        scout.duration = attr["session_wait"] + system["duration"] + SCOUT_MARGIN
    if system.get("queue"):
        scout.setattr("system.queue", system["queue"])
    env = dict(system.get("environment") or {})
    if attr.get("pythonpath"):
        env["PYTHONPATH"] = attr["pythonpath"]
    scout.environment = env
    if system.get("cwd"):
        scout.cwd = system["cwd"]
    scout_attr = dict(attr)
    scout_attr["pair"] = "scout"
    scout_attr["hold"] = False
    scout_attr["partner"] = int(partner)
    scout.setattr("system.qfw", scout_attr)
    scout.jobspec["resources"] = resources.add_device(
        classical_foothold(spec["resources"]),
        envelope.qpu,
        envelope.qpus,
        envelope.max_qubits,
    )
    try:
        jobid = flux.job.submit(handle, scout)
        handle.rpc(
            "job-manager.memo",
            {"id": int(partner), "volatile": False, "memo": {"qfw_scout": int(jobid)}},
        ).get()
    except Exception as error:
        flux.job.cancel(
            handle, partner, "flux-qfw: no scout for {}: {}".format(envelope.qpu, error)
        )
        print(
            "flux-qfw: could not submit the scout job for {}, so job {} was "
            "cancelled: {}".format(envelope.qpu, flux.job.JobID(partner).f58, error),
            file=sys.stderr,
        )
        return None
    print(
        "flux-qfw: scout job {} submitted for {}".format(
            flux.job.JobID(jobid).f58, envelope.qpu
        ),
        file=sys.stderr,
    )
    return int(jobid)


def classical_foothold(classical):
    """Return a one core request with the same levels as the classical one.

    Keeping the same levels means it matches the same graph. It never asks
    for a whole node, even when the classical job does.
    """
    entry = copy.deepcopy(classical[0])
    entry["count"] = 1
    entry.pop("exclusive", None)
    node = entry
    while node.get("with"):
        child = node["with"][0]
        child["count"] = 1
        child.pop("exclusive", None)
        node["with"] = [child]
        node = child
    return [entry]

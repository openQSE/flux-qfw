# flux-qfw

> QFw quantum devices as Fluxion resources.

flux-qfw is the Flux version of [qfw-slurm](https://github.com/openQSE/qfw-slurm). While the qfw-slurm reserves devices before Slurm grants the job, flux-qfw puts each QFw QPM into the Fluxion resource graph, next to the compute nodes, and lets Fluxion schedule it like a core. The flux submit (and similar) client is updated to ask for the device:

```bash
flux submit -N2 -n8 --qfw-qpu=nwqsim ./my_hybrid_app.sh
```

What happens next depends on control, because we do not control a second vendor queue.

- **A device we own and control.** Fluxion gives the job its nodes and the device together, in one match. If the device is busy, the job waits in the queue the same way it would for a busy core.
- **A device behind a vendor's queue.** A grant from Fluxion does not mean the vendor is ready. So the submit turns into a pair of jobs. The user's job goes in first, held with its nodes reserved. Then a small scout job goes in that names the user's job, holds the device in the graph, and waits for the vendor. When the vendor is ready, the scout releases the user's job and both run together.

Either way the device is a vertex in the graph and Fluxion decides who has it. This integration needs flux-core and the `hold` branch of [vsoch/flux-sched](https://github.com/vsoch/flux-sched), which adds the hold, the release RPC, and the `coschedule` queue policy. The plugins are tested end to end with a mock QPM. The DEFw backend that interacts to a real QPM is a port of the qfw-slurm gateway code and has not been tested yet.

## What is in here

| Piece | Where | What it does |
| --- | --- | --- |
| CLI plugin | `cli-plugins/qfw.py`, `flux_qfw/cli_plugin.py` | Adds the `--qfw-*` options to `flux run`, `submit`, `batch` and `alloc`. Adds the device to the job's resources, or submits the scout pair for an external queue. |
| Shell plugin | `flux_qfw/flux_plugins/flux-qfw.lua` | Runs in every job shell. Exports what the tasks need, like `QFW_RESERVATIONS` or the simulator hosts. |
| Scout | `flux_qfw/scout.py` | The program the scout job runs. Opens the vendor session and releases the user's job when it is ready. |
| Helper | `flux-qfw` | Command line tool with `devices`, `add`, `sessions`, `scout`, `register`, `unregister`, `memo`, `granted`, `status` and `plugins`. |
| Backends | `flux_qfw/qpm/` | `defw` talks to a real QPM. `mock` keeps reservations and sessions in small files for testing. |

## The resource shape

Each QPM is one `qdevice_<service_id>` vertex with one or more `qpu` children, and each `qpu` has a `qubit` pool as wide as the device. It sits under the cluster root, beside the racks and not under any node.

```text
/cluster0/rack0/c1/core0                      a compute core
/cluster0/qdevice_nwqsim0/qpu0                the NWQ-Sim device
/cluster0/qdevice_nwqsim0/qpu0/qubit0         a pool of 32 qubits
/cluster0/qdevice_fake-iqm0/qpu0              a vendor device with two leases
/cluster0/qdevice_fake-iqm0/qpu0/qubit0       of 20 qubits each
/cluster0/qdevice_fake-iqm0/qpu1
/cluster0/qdevice_fake-iqm0/qpu1/qubit0
```

The number of `qpu` vertices is how many jobs can hold the device at once. Use one for a device the site owns and should be exclusive. Use the number of concurrent leases for a vendor device. Fluxion does the admitting, so the QPM's own admission policy can be `unlimited`. A hybrid job keeps its normal classical request and gets a second top level entry for the device. The `qpu` asks for as many qubits as `--qfw-max-qubits`, which is one unless the user says more, so Fluxion only gives the job a device that wide. The plugin refuses a job wider than the device at submit.

```json
[
  {"type": "node", "count": 2, "with": [{"type": "slot", "count": 4, "label": "task",
     "with": [{"type": "core", "count": 1}]}]},
  {"type": "qdevice_nwqsim", "count": 1, "with": [{"type": "qpu", "count": 1, "exclusive": true,
     "with": [{"type": "qubit", "count": 5}]}]}
]
```

A hybrid job needs a node count, so always pass `-N`. Without it there is no node level to put beside the device, and the plugin refuses the job at submit. I am assuming it is not possible to run quantum work without some small amount of classical resources to submit it. The plugin also reads the live graph. If it finds levels between node and core, like a socket, it adds them to the request so Fluxion can match it.

## Devices we own

These are configured in the instance's `[qfw]` table, one entry per device.

```toml
[qfw]
launch-agent = "/usr/local/libexec/qfw-flux/flux-launch-agent"

[qfw.qpm.nwqsim]
scope = "job"

[qfw.qpm.shim-ornl-20q]
scope = "site"
```

There are two kinds.

**Job scope.** This is for a simulator. The job runs the simulator itself on its own nodes, so there is nothing to reserve. Every rank gets `QFW_QPU_SCOPE=job`, the job's hosts in `QFW_SIMULATOR_NODES`, and a launch agent so the MPI runtime starts its daemons with `flux exec` instead of ssh. QFw then runs exactly as it would on a workstation.

**Site scope.** This is for a QPM the site runs as a service. The job already holds the device in the graph, so the shell does not acquire anything. When the job starts it registers the job with the QPM, and when the job ends it unregisters it, even if the task fails. The QPM hands back the reservation id QFw needs. If the QPM refuses a job Fluxion already granted, the job fails with a message to set the QPM's admission policy to `unlimited`. Inside the job, `QFW_RESERVATIONS` looks like `[["shim-ornl-20q","<id>"]]`, which is the format QFw's `qfw-srun` uses.

## Devices behind a vendor queue

Add `queue = "external"` to the device's entry.

```toml
[qfw.qpm.fake-iqm]
scope = "site"
queue = "external"
```

The user still types `flux submit --qfw-qpu=fake-iqm`. Under the hood the plugin submits two jobs.

1. The **user's job** asks for its normal nodes and no device. It is submitted first, held. Under the `coschedule` policy Fluxion keeps its nodes reserved on every scheduling loop but does not start it. Other work can still run around it.
2. The **scout job** is submitted as soon as the user's job id comes back, with that id as its partner. The plugin memos the scout's id onto the user's job as `qfw_scout`. The scout asks for one core and the device. It waits in the queue like any other job, so it only runs while it holds a lease. That means the number of open vendor sessions can never be more than the number of leases.

The user's reserved nodes are off limits to the scout, so the scout needs a core outside them. The plugin checks the graph at submit and refuses a job that leaves no such core, like one that fills every node it could run on. The core can be on another node, or left over on one of the user's nodes when the job does not take whole nodes.

Once the scout starts, it opens a session on the vendor's queue and waits. When the session is ready it writes the reservation onto the user's job as a memo and releases the hold. The user's job starts right away, and its shell exports the reservation as `QFW_RESERVATIONS`. The scout keeps the device and the session until the user's job ends, then closes the session and exits.

The scout gives the vendor a fixed time to get ready, set with `session-wait` in the device's table or `--qfw-session-wait`, and one hour by default. If the session is not ready by then, the scout closes it and the user's job gets an exception, so its nodes are not reserved forever. The scout's own clock starts before the user's job, so its time limit is the session wait plus the user's duration plus a minute. That way the scout, and the session it keeps, outlive the user's job.

If the user cancels their job while it is held, the scout closes the session and exits. If the scout is cancelled, the user's job gets an exception. If the vendor refuses the session, the user's job gets an exception too. We are never left waiting forever, waiting for Godot. Run `flux-qfw sessions` to see sessions.

The `mock` backend can fake a vendor queue. `FLUX_QFW_MOCK_SESSION_DELAY` sets how long the fake vendor takes, and the default is five seconds. The `defw` backend does not support sessions yet.

## Adding devices to a cluster

Make R as usual, then add the devices before the instance starts.

```bash
flux R encode --hosts=flux-lead,c[1-8] --cores=0-3 --property=normal:1-8 > R.json
flux-qfw add --r R.json --output R-q.json nwqsim:32 fake-iqm=2:20
```

A device is `NAME[=QPUS][:QUBITS]`, so `fake-iqm=2:20` is two leases of 20 qubits each. Both default to one. Point `[resource] path` at `R-q.json` and set `noverify = true`. The `--r` option takes a plain R, an R that already has a scheduling graph, or a bare graph. If you build your own graph in Python, call `flux_qfw.resources.add_devices_to_graph()`.

You can also add a device to an instance that is already running.

```bash
flux-qfw add fake-iqm=2:20
```

This uses Fluxion's `add_subgraph` RPC and only the instance owner can run it. The new device shows up in Fluxion's graph and in the R of any job that gets it. It does not show up in the `resource.R` that Flux stored at boot.

Check the devices either way.

```bash
flux-qfw devices
```

## Submitting

```bash
export FLUX_CLI_PLUGINPATH=$(flux-qfw plugins)
flux run -N1 -n4 --qfw-qpu=nwqsim --qfw-max-qubits=5 --qfw-max-shots=1024 ./qfw_qiskit_simple.sh 5
```

These are the options. They match what qfw-slurm's `salloc --qpu` took.

| Option | What it means |
| --- | --- |
| `--qfw-qpu` | The device to use |
| `--qfw-qpus` | How many leases to take on it, default 1 |
| `--qfw-max-qubits` | The widest circuit, default 1. Fluxion matches the device's qubits against it, and the QPM gets it too |
| `--qfw-max-depth`, `--qfw-max-shots`, `--qfw-circ-count` | The size of the work, sent to the QPM when a reservation or session is made |
| `--qfw-one-q-gates`, `--qfw-two-q-gates`, `--qfw-measurements` | More size bounds for the QPM |
| `--qfw-workload-kind` | `hybrid` (default) or `quantum` |
| `--qfw-scope` | Override the device's scope, `job` or `site` |
| `--qfw-queue` | Override the device's queue, `owned` or `external` |
| `--qfw-session-wait` | How long the scout waits for the vendor, like `30m`. Overrides the device's `session-wait`, default `1h` |
| `--qfw-backend` | `defw` (default) or `mock` |
| `--qfw-no-reserve` | Schedule the device but do not tell the QPM |

Fluxion reads only the qubits. Its job is to decide who holds the device, and whether the device is wide enough. Whether the rest of the work fits is the QPM's call. The helper runs with the environment you submitted from. For the `defw` backend, activate QFw with `qfw-activate` first so it can find the venv and `QFW_SITE_CONFIG`.

## Tests

```bash
make unit      # no flux needed
make test      # also runs the t/ tests under flux start with Fluxion
```

## How this differs from qfw-slurm

- There is no gateway daemon, SPANK plugin, burst buffer or job submit Lua. The device is a resource and Fluxion allocates it.
- Vendor queues use a scout job and a held job, instead of reserving before the allocation is granted.
- There is no job identity check yet for site scope. The shell runs as the job owner and calls the QPM directly. That is fine for a device the site controls, but a multi user site will want a verified path.
- There are no heterogeneous jobs. A hybrid job is one job with two resource entries.

## License

MIT. See LICENSE, COPYRIGHT and NOTICE.

"""Read and change the resource graph of a running Flux instance.

resources.py knows the shape of a device. This module asks a live instance
what it has, and adds devices to it.
"""

import json
import os
import subprocess

import flux
import flux.kvs

from . import resources


def live_graph(handle=None, criteria="status=up or status=down"):
    """Return the graph Fluxion is scheduling from right now.

    This includes devices added while the instance runs, which the R stored
    at boot does not. Only the instance owner can ask.
    """
    handle = handle or flux.Flux()
    resp = handle.rpc(
        "sched-fluxion-resource.find", {"criteria": criteria, "format": "jgf"}
    ).get()
    doc = resp.get("R")
    if isinstance(doc, (str, bytes)):
        doc = json.loads(doc)
    if not isinstance(doc, dict):
        raise ValueError("find RPC returned no graph")
    return doc


def load_instance_graph(handle=None):
    """Return the instance's scheduling graph.

    FLUX_QFW_GRAPH can name a file, which the tests use. Otherwise this tries
    the live Fluxion graph, then falls back to the R that Flux stored at boot.
    """
    path = os.environ.get("FLUX_QFW_GRAPH")
    if path:
        with open(path) as fh:
            return json.load(fh)
    handle = handle or flux.Flux()
    try:
        return live_graph(handle)
    except Exception:
        pass
    r = flux.kvs.get(handle, "resource.R")
    if isinstance(r, (str, bytes)):
        r = json.loads(r)
    if "scheduling" not in r:
        raise ValueError(
            "the instance R has no scheduling graph; is resource.path set?"
        )
    return r


def add_devices(specs, handle=None):
    """Add devices to a running instance.

    specs is a list of device specs (see resources.parse_spec). Devices
    already present are skipped. The live graph's root is sent along with
    the new vertices so Fluxion attaches them under it. Only the instance
    owner can do this.
    Returns the service ids that were added.
    """
    handle = handle or flux.Flux()
    live = live_graph(handle)
    graph = resources.graph_of(live)
    present = resources.devices_in(live)
    todo = [s for s in map(resources.parse_spec, specs) if s[0] not in present]
    if not todo:
        return []
    root = resources.root_of(graph)
    first = max(int(n["id"]) for n in graph["nodes"]) + 1
    nodes, edges = resources.device_subgraph(root, first, todo)
    handle.rpc(
        "sched-fluxion-resource.add_subgraph",
        {"subgraph": json.dumps({"graph": {"nodes": [root] + nodes, "edges": edges}})},
    ).get()
    return [s[0] for s in todo]


def r_from_config(path):
    """Return the R that flux R parse-config builds from a broker config."""
    result = subprocess.run(
        ["flux", "R", "parse-config", path],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError(
            "flux R parse-config {} failed: {}".format(path, result.stderr.strip())
        )
    return json.loads(result.stdout)


def encode_r(doc):
    """Return a plain R with a scheduling graph added, using flux ion-R encode."""
    if "graph" in doc or "scheduling" in doc:
        return doc
    result = subprocess.run(
        ["flux", "ion-R", "encode"],
        input=json.dumps(doc),
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError(
            "flux ion-R encode failed, is flux-sched installed? {}".format(
                result.stderr.strip()
            )
        )
    return json.loads(result.stdout)

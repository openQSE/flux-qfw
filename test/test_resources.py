import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from flux_qfw import resources  # noqa: E402


def classical(nodes=1, cores=4):
    return [
        {
            "type": "node",
            "count": nodes,
            "with": [
                {
                    "type": "slot",
                    "count": cores,
                    "label": "task",
                    "with": [{"type": "core", "count": 1}],
                }
            ],
        }
    ]


def graph_with(devices):
    nodes = [
        {
            "id": "0",
            "metadata": {"type": "cluster", "paths": {"containment": "/cluster0"}},
        }
    ]
    edges = []
    nid = 1
    for sid, count in devices.items():
        did = str(nid)
        nid += 1
        nodes.append(
            {
                "id": did,
                "metadata": {
                    "type": resources.device_type(sid),
                    "paths": {
                        "containment": "/cluster0/" + resources.device_type(sid) + "0"
                    },
                },
            }
        )
        edges.append({"source": "0", "target": did})
        for i in range(count):
            qid = str(nid)
            nid += 1
            nodes.append(
                {
                    "id": qid,
                    "metadata": {
                        "type": "qpu",
                        "paths": {"containment": "/cluster0/x/qpu%d" % i},
                    },
                }
            )
            edges.append({"source": did, "target": qid})
    return {"scheduling": {"graph": {"nodes": nodes, "edges": edges}}}


class ShapeTest(unittest.TestCase):
    def test_device_type_round_trip(self):
        for sid in ("nwqsim", "iqm-ornl-20q", "fake-iqm", "shim-ornl-20q"):
            self.assertEqual(
                resources.service_id_from_type(resources.device_type(sid)), sid
            )
        self.assertEqual(resources.device_type("a b/c"), "qdevice_a_b_c")
        self.assertIsNone(resources.service_id_from_type("node"))

    def test_add_device_keeps_classical_request(self):
        before = classical()
        after = resources.add_device(before, "nwqsim")
        self.assertEqual(after[0], before[0], "classical entry must be untouched")
        self.assertEqual(len(after), 2)
        dev = after[1]
        self.assertEqual(dev["type"], "qdevice_nwqsim")
        self.assertEqual(
            dev["with"][0],
            {
                "type": "qpu",
                "count": 1,
                "exclusive": True,
                "with": [{"type": "qubit", "count": 1}],
            },
        )
        wide = resources.add_device(before, "nwqsim", qubits=5)
        self.assertEqual(wide[1]["with"][0]["with"], [{"type": "qubit", "count": 5}])

    def test_add_device_refuses_duplicate_and_bad_input(self):
        once = resources.add_device(classical(), "nwqsim")
        with self.assertRaises(ValueError):
            resources.add_device(once, "nwqsim")
        with self.assertRaises(ValueError):
            resources.add_device([], "nwqsim")
        bare = [{"type": "slot", "count": 1, "with": [{"type": "core", "count": 1}]}]
        with self.assertRaisesRegex(ValueError, "-N"):
            resources.add_device(bare, "nwqsim")
        with self.assertRaises(ValueError):
            resources.device_request("nwqsim", 0)

    def test_devices_in_and_granted(self):
        doc = graph_with({"nwqsim": 1, "fake-iqm": 2})
        self.assertEqual(resources.devices_in(doc), {"nwqsim": 1, "fake-iqm": 2})
        self.assertEqual(
            [sid for sid, _ in resources.granted(doc)], ["fake-iqm", "nwqsim"]
        )

    def test_mirror_path_on_socket_graph(self):
        # cluster, rack, node, socket, core, the same levels as flux-sched's tiny.json
        nodes = [
            {"id": str(i), "metadata": {"type": t, "paths": {"containment": "/x"}}}
            for i, t in enumerate(["cluster", "rack", "node", "socket", "core"])
        ]
        edges = [{"source": str(i), "target": str(i + 1)} for i in range(4)]
        doc = {"graph": {"nodes": nodes, "edges": edges}}
        self.assertEqual(resources.node_to_core_path(doc), ["socket"])
        flat = classical(cores=2)
        out = resources.mirror_path(flat, ["socket"])
        self.assertEqual(out[0]["type"], "node")
        self.assertEqual(out[0]["with"][0]["type"], "socket")
        self.assertEqual(out[0]["with"][0]["with"][0]["type"], "slot")
        self.assertEqual(
            out[0]["with"][0]["with"][0]["with"], [{"type": "core", "count": 1}]
        )
        # A graph from a plain R has no levels in between, so nothing changes.
        self.assertEqual(resources.mirror_path(flat, []), flat)
        rv1 = {
            "graph": {
                "nodes": nodes[:1] + nodes[2:3] + nodes[4:],
                "edges": [
                    {"source": "0", "target": "2"},
                    {"source": "2", "target": "4"},
                ],
            }
        }
        self.assertEqual(resources.node_to_core_path(rv1), [])

    def test_device_spec(self):
        spec = resources.device_spec("nwqsim", 2)
        self.assertEqual(spec["type"], "qdevice_nwqsim")
        self.assertEqual(spec["with"][0]["count"], 2)
        self.assertEqual(spec["properties"]["service_id"], "nwqsim")
        json.dumps(spec)

    def test_add_devices_to_graph(self):
        root = {
            "id": "0",
            "metadata": {"type": "cluster", "paths": {"containment": "/cluster0"}},
        }
        node = {
            "id": "1",
            "metadata": {"type": "node", "paths": {"containment": "/cluster0/c1"}},
        }
        doc = {
            "graph": {"nodes": [root, node], "edges": [{"source": "0", "target": "1"}]}
        }
        added = resources.add_devices_to_graph(doc, ["nwqsim:32", ("fake-iqm", 2, 20)])
        self.assertEqual(added, ["nwqsim", "fake-iqm"])
        self.assertEqual(resources.devices_in(doc), {"nwqsim": 1, "fake-iqm": 2})
        paths = sorted(
            n["metadata"]["paths"]["containment"] for n in doc["graph"]["nodes"]
        )
        self.assertEqual(
            paths,
            [
                "/cluster0",
                "/cluster0/c1",
                "/cluster0/qdevice_fake-iqm0",
                "/cluster0/qdevice_fake-iqm0/qpu0",
                "/cluster0/qdevice_fake-iqm0/qpu0/qubit0",
                "/cluster0/qdevice_fake-iqm0/qpu1",
                "/cluster0/qdevice_fake-iqm0/qpu1/qubit0",
                "/cluster0/qdevice_nwqsim0",
                "/cluster0/qdevice_nwqsim0/qpu0",
                "/cluster0/qdevice_nwqsim0/qpu0/qubit0",
            ],
        )
        self.assertEqual(resources.device_qubits(doc), {"nwqsim": 32, "fake-iqm": 20})
        # New vertex ids continue after the highest one and stay unique.
        ids = [int(n["id"]) for n in doc["graph"]["nodes"]]
        self.assertEqual(sorted(ids), list(range(len(ids))))
        # Every new vertex sits under the root or a device, never under a node.
        parents = {e["target"]: e["source"] for e in doc["graph"]["edges"]}
        self.assertNotIn("1", [parents[str(i)] for i in ids if i > 1])
        # A second call adds nothing.
        self.assertEqual(resources.add_devices_to_graph(doc, [("nwqsim", 1)]), [])
        self.assertEqual(resources.devices_in(doc), {"nwqsim": 1, "fake-iqm": 2})
        json.dumps(doc)

    def test_parse_spec(self):
        self.assertEqual(resources.parse_spec("nwqsim"), ("nwqsim", 1, 1))
        self.assertEqual(resources.parse_spec("fake-iqm=2"), ("fake-iqm", 2, 1))
        self.assertEqual(resources.parse_spec("fake-iqm=2:20"), ("fake-iqm", 2, 20))
        self.assertEqual(resources.parse_spec("nwqsim:32"), ("nwqsim", 1, 32))
        self.assertEqual(resources.parse_spec(("nwqsim", 1)), ("nwqsim", 1, 1))
        with self.assertRaises(ValueError):
            resources.parse_spec("nwqsim:0")

    def test_device_qubits_without_pool(self):
        # A graph built before qubits were modeled has none to match.
        self.assertEqual(
            resources.device_qubits(graph_with({"nwqsim": 1})), {"nwqsim": None}
        )


def cluster(*cores, props=None):
    """Return a graph with one node per entry in cores, each with that many cores."""
    nodes = [{"id": "0", "metadata": {"type": "cluster"}}]
    edges = []
    nid = 1
    for i, n in enumerate(cores):
        meta = {"type": "node", "properties": (props or {}).get(i, {})}
        nodes.append({"id": str(nid), "metadata": meta})
        edges.append({"source": "0", "target": str(nid)})
        node, nid = nid, nid + 1
        for _ in range(n):
            nodes.append({"id": str(nid), "metadata": {"type": "core"}})
            edges.append({"source": str(node), "target": str(nid)})
            nid += 1
    return {"graph": {"nodes": nodes, "edges": edges}}


class ScoutRoomTest(unittest.TestCase):
    def test_spare_core_on_the_node(self):
        self.assertIsNone(resources.fits_with_scout(cluster(4), classical(1, 3)))
        self.assertIn("no core", resources.fits_with_scout(cluster(4), classical(1, 4)))

    def test_spare_node(self):
        self.assertIsNone(resources.fits_with_scout(cluster(4, 4), classical(1, 4)))
        self.assertIsNotNone(resources.fits_with_scout(cluster(4, 4), classical(2, 4)))
        # A small node no classical node would fit still holds the scout.
        self.assertIsNone(resources.fits_with_scout(cluster(4, 4, 1), classical(2, 4)))

    def test_whole_nodes_need_another_node(self):
        whole = classical(1, 2)
        whole[0]["exclusive"] = True
        self.assertIn("whole nodes", resources.fits_with_scout(cluster(4), whole))
        self.assertIsNone(resources.fits_with_scout(cluster(4, 4), whole))

    def test_too_few_nodes(self):
        self.assertIn(
            "asks for 3", resources.fits_with_scout(cluster(4, 4), classical(3, 1))
        )

    def test_properties_narrow_the_nodes(self):
        doc = cluster(4, 4, props={0: {"normal": ""}})
        self.assertIsNone(resources.fits_with_scout(doc, classical(1, 4)))
        self.assertIsNotNone(
            resources.fits_with_scout(doc, classical(1, 4), {"properties": ["normal"]})
        )
        # Anything but a plain property list is left to the scheduler.
        self.assertIsNone(
            resources.fits_with_scout(
                doc, classical(1, 4), {"not": [{"properties": ["normal"]}]}
            )
        )


if __name__ == "__main__":
    unittest.main()

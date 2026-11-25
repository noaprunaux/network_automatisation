"""Tests unitaires de l'étape 2 (générateur NetBox -> Containerlab)."""
from __future__ import annotations

from pathlib import Path

import yaml

from webui.generator import (
    generate_clab_dict,
    generate_clab_yaml,
    netbox_to_clab_interface,
    write_clab_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _sample_netauto_topology() -> dict:
    """Topologie NetBox équivalente au lab de référence `netauto`."""
    return {
        "name": "netauto",
        "site_slug": "tp-automatisation",
        "readonly": True,
        "nodes": [
            {
                "id": "r1",
                "device_name": "clab-netauto-r1",
                "role": "router",
                "mgmt_ip": "172.20.20.11/24",
                "interfaces": [
                    {"name": "mgmt0", "mgmt_only": True, "ips": ["172.20.20.11/24"]},
                    {"name": "ethernet-1/1", "mgmt_only": False, "ips": ["192.168.10.1/24"]},
                    {"name": "ethernet-1/2", "mgmt_only": False, "ips": ["10.0.12.1/30"]},
                ],
                "context": {"asn": 65001, "router_id": "10.0.12.1", "bgp_neighbors": []},
            },
            {
                "id": "r2",
                "device_name": "clab-netauto-r2",
                "role": "router",
                "mgmt_ip": "172.20.20.12/24",
                "interfaces": [
                    {"name": "mgmt0", "mgmt_only": True, "ips": ["172.20.20.12/24"]},
                    {"name": "ethernet-1/1", "mgmt_only": False, "ips": ["192.168.20.1/24"]},
                    {"name": "ethernet-1/2", "mgmt_only": False, "ips": ["10.0.12.2/30"]},
                ],
                "context": {"asn": 65002, "router_id": "10.0.12.2", "bgp_neighbors": []},
            },
            {
                "id": "sw1",
                "device_name": "clab-netauto-sw1",
                "role": "switch",
                "mgmt_ip": "172.20.20.21/24",
                "interfaces": [
                    {"name": "mgmt0", "mgmt_only": True, "ips": ["172.20.20.21/24"]},
                    {"name": "ethernet-1/1", "mgmt_only": False, "ips": []},
                    {"name": "ethernet-1/2", "mgmt_only": False, "ips": []},
                ],
                "context": {"mac_vrf": "lan-a"},
            },
            {
                "id": "sw2",
                "device_name": "clab-netauto-sw2",
                "role": "switch",
                "mgmt_ip": "172.20.20.22/24",
                "interfaces": [
                    {"name": "mgmt0", "mgmt_only": True, "ips": ["172.20.20.22/24"]},
                    {"name": "ethernet-1/1", "mgmt_only": False, "ips": []},
                    {"name": "ethernet-1/2", "mgmt_only": False, "ips": []},
                ],
                "context": {"mac_vrf": "lan-b"},
            },
            {
                "id": "client1",
                "device_name": "clab-netauto-client1",
                "role": "container",
                "mgmt_ip": "172.20.20.31/24",
                "interfaces": [
                    {"name": "eth0", "mgmt_only": True, "ips": ["172.20.20.31/24"]},
                    {"name": "eth1", "mgmt_only": False, "ips": ["192.168.10.10/24"]},
                ],
                "context": {},
            },
            {
                "id": "client2",
                "device_name": "clab-netauto-client2",
                "role": "container",
                "mgmt_ip": "172.20.20.32/24",
                "interfaces": [
                    {"name": "eth0", "mgmt_only": True, "ips": ["172.20.20.32/24"]},
                    {"name": "eth1", "mgmt_only": False, "ips": ["192.168.20.10/24"]},
                ],
                "context": {},
            },
        ],
        "links": [
            {"id": 1, "from": "client1", "from_interface": "eth1", "to": "sw1", "to_interface": "ethernet-1/1"},
            {"id": 2, "from": "sw1", "from_interface": "ethernet-1/2", "to": "r1", "to_interface": "ethernet-1/1"},
            {"id": 3, "from": "r1", "from_interface": "ethernet-1/2", "to": "r2", "to_interface": "ethernet-1/2"},
            {"id": 4, "from": "r2", "from_interface": "ethernet-1/1", "to": "sw2", "to_interface": "ethernet-1/2"},
            {"id": 5, "from": "sw2", "from_interface": "ethernet-1/1", "to": "client2", "to_interface": "eth1"},
        ],
    }


def test_interface_conversion():
    assert netbox_to_clab_interface("ethernet-1/1") == "e1-1"
    assert netbox_to_clab_interface("ethernet-1/12") == "e1-12"
    assert netbox_to_clab_interface("eth1") == "eth1"


def test_equivalent_to_netauto_lab_yml():
    """Le fichier généré pour netauto doit être équivalent au netauto_lab.yml d'origine."""
    orig_path = REPO_ROOT / "netauto_lab.yml.orig"
    ref_path = orig_path if orig_path.is_file() else REPO_ROOT / "netauto_lab.yml"
    ref = yaml.safe_load(ref_path.read_text(encoding="utf-8"))
    gen = generate_clab_dict(_sample_netauto_topology())

    assert gen["name"] == ref["name"]
    assert gen["mgmt"] == ref["mgmt"]

    # Mêmes nœuds, mêmes kinds et mêmes IP de management
    assert set(gen["topology"]["nodes"].keys()) == set(ref["topology"]["nodes"].keys())
    for name, ref_node in ref["topology"]["nodes"].items():
        gen_node = gen["topology"]["nodes"][name]
        assert gen_node["kind"] == ref_node["kind"]
        assert gen_node["mgmt-ipv4"] == ref_node["mgmt-ipv4"]

    # Clients : IP eth1 + route par défaut vers la passerelle du segment
    assert gen["topology"]["nodes"]["client1"]["exec"] == [
        "ip addr add 192.168.10.10/24 dev eth1",
        "ip route replace default via 192.168.10.1 dev eth1",
    ]
    assert gen["topology"]["nodes"]["client2"]["exec"] == [
        "ip addr add 192.168.20.10/24 dev eth1",
        "ip route replace default via 192.168.20.1 dev eth1",
    ]

    # Mêmes liens (comparaison indépendante de l'ordre des extrémités)
    ref_links = {frozenset(item["endpoints"]) for item in ref["topology"]["links"]}
    gen_links = {frozenset(item["endpoints"]) for item in gen["topology"]["links"]}
    assert gen_links == ref_links


def test_deterministic_yaml():
    """Même topologie dans un ordre différent => YAML strictement identique."""
    topo_a = _sample_netauto_topology()
    topo_b = _sample_netauto_topology()
    topo_b["nodes"] = list(reversed(topo_b["nodes"]))
    topo_b["links"] = [
        {
            "id": link["id"],
            "from": link["to"],
            "from_interface": link["to_interface"],
            "to": link["from"],
            "to_interface": link["from_interface"],
        }
        for link in reversed(topo_b["links"])
    ]

    assert generate_clab_yaml(topo_a) == generate_clab_yaml(topo_b)


def test_multi_switch_gateway_discovery():
    """Un client derrière deux switchs en cascade trouve bien la passerelle du routeur."""
    topo = {
        "name": "chain",
        "nodes": [
            {
                "id": "r1",
                "role": "router",
                "mgmt_ip": "172.20.30.11/24",
                "interfaces": [{"name": "ethernet-1/1", "mgmt_only": False, "ips": ["192.168.50.1/24"]}],
            },
            {"id": "sw1", "role": "switch", "mgmt_ip": "172.20.30.21/24", "interfaces": []},
            {"id": "sw2", "role": "switch", "mgmt_ip": "172.20.30.22/24", "interfaces": []},
            {
                "id": "c1",
                "role": "container",
                "mgmt_ip": "172.20.30.31/24",
                "interfaces": [{"name": "eth1", "mgmt_only": False, "ips": ["192.168.50.10/24"]}],
            },
        ],
        "links": [
            {"id": 1, "from": "c1", "from_interface": "eth1", "to": "sw2", "to_interface": "ethernet-1/1"},
            {"id": 2, "from": "sw2", "from_interface": "ethernet-1/2", "to": "sw1", "to_interface": "ethernet-1/1"},
            {"id": 3, "from": "sw1", "from_interface": "ethernet-1/2", "to": "r1", "to_interface": "ethernet-1/1"},
        ],
    }
    gen = generate_clab_dict(topo)
    assert gen["topology"]["nodes"]["c1"]["exec"] == [
        "ip addr add 192.168.50.10/24 dev eth1",
        "ip route replace default via 192.168.50.1 dev eth1",
    ]


def test_write_clab_file_idempotent(tmp_path: Path):
    """La deuxième écriture sans changement produit un diff vide."""
    topo = _sample_netauto_topology()
    path1, yaml1, diff1 = write_clab_file(topo, tmp_path)
    assert path1 == tmp_path / "netauto" / "netauto.clab.yml"
    assert path1.is_file()
    assert diff1 != ""

    path2, yaml2, diff2 = write_clab_file(topo, tmp_path)
    assert path2 == path1
    assert yaml2 == yaml1
    assert diff2 == ""

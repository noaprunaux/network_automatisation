"""Tests unitaires de l'étape 3 (validation, IPAM simulé, dérivation BGP et API écriture)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from webui import main
from webui.generator import generate_clab_dict
from webui.topology import (
    LabPayload,
    TopologyValidationError,
    plan_lab_topology,
    validate_lab_payload,
)


class FakeIpamAllocator:
    """Allocateur IPAM en mémoire pour tester sans serveur NetBox réel."""

    def __init__(self) -> None:
        self._mgmt_idx = 32
        self._p2p_idx = 0
        self._lan_idx = 1

    def allocate_mgmt_prefix(self, lab_name: str) -> str:
        prefix = f"172.20.{self._mgmt_idx}.0/24"
        self._mgmt_idx += 1
        return prefix

    def allocate_p2p_prefix(self, lab_name: str, link_label: str) -> str:
        prefix = f"10.100.0.{self._p2p_idx * 4}/30"
        self._p2p_idx += 1
        return prefix

    def allocate_lan_prefix(self, lab_name: str, lan_label: str) -> str:
        prefix = f"192.168.{self._lan_idx}.0/24"
        self._lan_idx += 1
        return prefix


def _demo_payload() -> LabPayload:
    return LabPayload.model_validate({
        "name": "demo",
        "nodes": [
            {"id": "r1", "role": "router"},
            {"id": "r2", "role": "router"},
            {"id": "sw1", "role": "switch"},
            {"id": "sw2", "role": "switch"},
            {"id": "client1", "role": "container"},
            {"id": "client2", "role": "container"},
        ],
        "links": [
            {"from": "client1", "to": "sw1"},
            {"from": "sw1", "to": "r1"},
            {"from": "r1", "to": "r2"},
            {"from": "r2", "to": "sw2"},
            {"from": "sw2", "to": "client2"},
        ],
    })


def test_validation_rejects_protected_and_invalid_names():
    p = _demo_payload()
    p.name = "netauto"
    with pytest.raises(TopologyValidationError, match="réservé"):
        validate_lab_payload(p)

    # Autorisé explicitement lors d'une mise à jour de netauto
    validate_lab_payload(p, allow_netauto=True)

    p.name = "Demo_Bad"
    with pytest.raises(TopologyValidationError, match="Nom de lab invalide"):
        validate_lab_payload(p)


def test_validation_rejects_topology_errors():
    # Pas de routeur
    with pytest.raises(TopologyValidationError, match="au moins un routeur"):
        validate_lab_payload(LabPayload.model_validate({
            "name": "demo",
            "nodes": [{"id": "sw1", "role": "switch"}],
            "links": [],
        }))

    # Lien client <-> client
    with pytest.raises(TopologyValidationError, match="deux clients"):
        validate_lab_payload(LabPayload.model_validate({
            "name": "demo",
            "nodes": [
                {"id": "r1", "role": "router"},
                {"id": "c1", "role": "container"},
                {"id": "c2", "role": "container"},
            ],
            "links": [{"from": "c1", "to": "c2"}],
        }))

    # Client déconnecté
    with pytest.raises(TopologyValidationError, match="exactement un switch ou un routeur"):
        validate_lab_payload(LabPayload.model_validate({
            "name": "demo",
            "nodes": [
                {"id": "r1", "role": "router"},
                {"id": "c1", "role": "container"},
            ],
            "links": [],
        }))

    # IP mgmt dans 172.20.20.0/24 réservé à netauto
    with pytest.raises(TopologyValidationError, match="172.20.20.0/24"):
        validate_lab_payload(LabPayload.model_validate({
            "name": "demo",
            "nodes": [{"id": "r1", "role": "router", "mgmt_ip": "172.20.20.99/24"}],
            "links": [],
        }))

    # Plus de 10 nœuds
    with pytest.raises(TopologyValidationError, match="Nombre maximum"):
        validate_lab_payload(LabPayload.model_validate({
            "name": "demo",
            "nodes": [{"id": f"r{i}", "role": "router"} for i in range(1, 12)],
            "links": [],
        }))


def test_plan_demo_lab_ipam_and_bgp_derivation():
    planned = plan_lab_topology(_demo_payload(), FakeIpamAllocator())
    nodes = {n["id"]: n for n in planned["nodes"]}

    # Préfixe mgmt dédié (jamais 172.20.20.0/24)
    assert planned["mgmt_prefix"] == "172.20.32.0/24"
    assert nodes["r1"]["mgmt_ip"] == "172.20.32.11/24"
    assert nodes["r2"]["mgmt_ip"] == "172.20.32.12/24"

    # ASN et BGP dérivés automatiquement
    assert nodes["r1"]["context"]["asn"] == 65001
    assert nodes["r2"]["context"]["asn"] == 65002
    assert nodes["r1"]["context"]["bgp_neighbors"] == [{"ip": "10.100.0.2", "peer_as": 65002}]
    assert nodes["r2"]["context"]["bgp_neighbors"] == [{"ip": "10.100.0.1", "peer_as": 65001}]
    assert nodes["r1"]["context"]["router_id"] == "10.100.0.1"
    assert nodes["r2"]["context"]["router_id"] == "10.100.0.2"

    # Switchs mac-vrf
    assert nodes["sw1"]["context"] == {"mac_vrf": "lan-1"}
    assert nodes["sw2"]["context"] == {"mac_vrf": "lan-2"}

    # Clients .10 sur leur LAN respectif
    c1_eth1 = [i for i in nodes["client1"]["interfaces"] if i["name"] == "eth1"][0]
    c2_eth1 = [i for i in nodes["client2"]["interfaces"] if i["name"] == "eth1"][0]
    assert c1_eth1["ips"] == ["192.168.1.10/24"]
    assert c2_eth1["ips"] == ["192.168.2.10/24"]

    # Compatible directement avec le générateur Containerlab
    clab = generate_clab_dict(planned)
    assert clab["mgmt"]["ipv4-subnet"] == "172.20.32.0/24"
    assert clab["topology"]["nodes"]["client1"]["exec"] == [
        "ip addr add 192.168.1.10/24 dev eth1",
        "ip route replace default via 192.168.1.1 dev eth1",
    ]


def test_manual_overrides():
    payload = LabPayload.model_validate({
        "name": "custom",
        "nodes": [
            {"id": "r1", "role": "router", "asn": 65100, "router_id": "1.1.1.1", "mgmt_ip": "172.20.32.50/24"},
            {"id": "r2", "role": "router"},
            {"id": "sw1", "role": "switch", "mac_vrf": "prod-lan"},
            {"id": "c1", "role": "container", "data_ip": "192.168.1.99/24"},
        ],
        "links": [
            {"from": "r1", "to": "r2"},
            {"from": "r1", "to": "sw1"},
            {"from": "sw1", "to": "c1"},
        ],
    })
    planned = plan_lab_topology(payload, FakeIpamAllocator())
    nodes = {n["id"]: n for n in planned["nodes"]}

    assert nodes["r1"]["context"]["asn"] == 65100
    assert nodes["r1"]["context"]["router_id"] == "1.1.1.1"
    assert nodes["r1"]["mgmt_ip"] == "172.20.32.50/24"
    assert nodes["r2"]["context"]["asn"] == 65001
    assert nodes["r2"]["context"]["bgp_neighbors"][0]["peer_as"] == 65100
    assert nodes["sw1"]["context"]["mac_vrf"] == "prod-lan"
    c1_eth1 = [i for i in nodes["c1"]["interfaces"] if i["name"] == "eth1"][0]
    assert c1_eth1["ips"] == ["192.168.1.99/24"]


def test_api_protects_netauto_delete_and_validates(monkeypatch):
    from webui.runner import Job

    client = TestClient(main.app)

    # DELETE sur netauto toujours refusé (403)
    resp = client.delete("/api/labs/netauto")
    assert resp.status_code == 403

    # DELETE sur un autre lab délègue à job_manager.destroy_lab
    monkeypatch.setattr(
        main.job_manager,
        "destroy_lab",
        lambda name: {"deleted": name, "containerlab_destroyed": True},
    )
    resp = client.delete("/api/labs/demo")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "demo", "containerlab_destroyed": True}

    # POST avec name=netauto refusé (422)
    resp = client.post("/api/labs", json={
        "name": "netauto",
        "nodes": [{"id": "r1", "role": "router"}],
        "links": [],
    })
    assert resp.status_code == 422

    # POST /api/labs/validate valide un payload correct
    resp = client.post("/api/labs/validate", json=_demo_payload().model_dump(by_alias=True))
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}

    # Masquage des secrets dans les logs SSE
    job = Job(id="test1", lab="demo", mode="preview", _secrets=["nbt_secret_token", "superpass"])
    job.log("Token=nbt_secret_token pass=superpass")
    assert "nbt_secret_token" not in job._log_lines[0]
    assert "superpass" not in job._log_lines[0]
    assert job._log_lines[0] == "Token=*** pass=***"

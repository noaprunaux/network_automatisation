"""Tests unitaires de l'étape 6 (inspection JSON-RPC et ping client)."""
from __future__ import annotations

import pytest

from webui import inspector


class FakeNB:
    def get_lab_topology(self, lab_name: str):
        return {
            "name": lab_name,
            "nodes": [
                {
                    "id": "r1",
                    "device_name": f"clab-{lab_name}-r1",
                    "role": "router",
                    "mgmt_ip": "172.20.20.11/24",
                    "interfaces": [
                        {"name": "mgmt0", "mgmt_only": True, "ips": ["172.20.20.11/24"]},
                        {"name": "ethernet-1/1", "mgmt_only": False, "ips": ["192.168.10.1/24"]},
                    ],
                    "context": {"bgp_neighbors": [{"ip": "10.0.12.2", "peer_as": 65002}]},
                },
                {
                    "id": "client1",
                    "device_name": f"clab-{lab_name}-client1",
                    "role": "container",
                    "mgmt_ip": "172.20.20.31/24",
                    "interfaces": [{"name": "eth1", "mgmt_only": False, "ips": ["192.168.10.10/24"]}],
                },
                {
                    "id": "client2",
                    "device_name": f"clab-{lab_name}-client2",
                    "role": "container",
                    "mgmt_ip": "172.20.20.32/24",
                    "interfaces": [{"name": "eth1", "mgmt_only": False, "ips": ["192.168.20.10/24"]}],
                },
            ],
            "links": [],
        }


def test_live_status_parses_jsonrpc(monkeypatch):
    monkeypatch.setattr(inspector, "load_vault_secrets", lambda: {"vault_srl_user": "admin", "vault_srl_password": "x"})
    monkeypatch.setattr(inspector, "_srl_jsonrpc_get", lambda ip, u, p, paths: ["up", "enable", "established"])

    status = inspector.get_lab_live_status("netauto", client=FakeNB())
    assert status["all_ok"] is True
    assert status["devices"][0]["interfaces"][0]["oper_state"] == "up"
    assert status["devices"][0]["bgp"][0]["session_state"] == "established"


def test_ping_rejects_same_client():
    with pytest.raises(ValueError, match="différents"):
        inspector.run_client_ping("netauto", "client1", "client1", client=FakeNB())

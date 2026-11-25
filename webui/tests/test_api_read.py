"""Tests unitaires de l'étape 1 (lecture NetBox et endpoints FastAPI)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from webui import main
from webui.netbox_client import LabNotFoundError, lab_to_site_slug, short_node_name, site_slug_to_lab


class FakeNetBoxClient:
    """Faux client NetBox pour tester l'API sans dépendre du serveur réel."""

    def list_labs(self):
        return [
            {
                "name": "netauto",
                "site_slug": "tp-automatisation",
                "display_name": "TP Automatisation",
                "readonly": True,
                "device_count": 6,
            }
        ]

    def get_lab_topology(self, lab_name: str):
        if lab_name != "netauto":
            raise LabNotFoundError(f"Lab '{lab_name}' introuvable.")
        return {
            "name": "netauto",
            "site_slug": "tp-automatisation",
            "display_name": "TP Automatisation",
            "readonly": True,
            "nodes": [
                {
                    "id": "r1",
                    "device_name": "clab-netauto-r1",
                    "role": "router",
                    "device_type": "sr-linux",
                    "mgmt_ip": "172.20.20.11/24",
                    "interfaces": [
                        {"id": 1, "name": "mgmt0", "mgmt_only": True, "ips": ["172.20.20.11/24"]},
                        {"id": 2, "name": "ethernet-1/1", "mgmt_only": False, "ips": ["192.168.10.1/24"]},
                    ],
                    "context": {"asn": 65001, "router_id": "10.0.12.1", "bgp_neighbors": []},
                }
            ],
            "links": [],
        }


def test_mapping_helpers():
    assert lab_to_site_slug("netauto") == "tp-automatisation"
    assert lab_to_site_slug("demo") == "demo"
    assert site_slug_to_lab("tp-automatisation") == "netauto"
    assert site_slug_to_lab("demo") == "demo"
    assert short_node_name("clab-netauto-r1", "netauto") == "r1"
    assert short_node_name("other-node", "netauto") == "other-node"


def test_api_endpoints(monkeypatch):
    monkeypatch.setattr(main, "get_netbox_client", lambda: FakeNetBoxClient())
    client = TestClient(main.app)

    assert client.get("/api/health").json() == {"status": "ok"}

    labs_resp = client.get("/api/labs")
    assert labs_resp.status_code == 200
    assert labs_resp.json()[0]["name"] == "netauto"
    assert labs_resp.json()[0]["readonly"] is True

    topo_resp = client.get("/api/labs/netauto")
    assert topo_resp.status_code == 200
    assert topo_resp.json()["nodes"][0]["id"] == "r1"

    missing_resp = client.get("/api/labs/inconnu")
    assert missing_resp.status_code == 404

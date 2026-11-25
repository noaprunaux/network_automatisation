#!/usr/bin/env python3
"""Remplit NetBox avec la topologie du lab : interfaces, IP, câbles, IP de
management (primary IP) et données BGP / mac-vrf (config context local).

Idempotent : on peut le relancer sans créer de doublons.
Prérequis : les équipements existent déjà dans NetBox (clab-netauto-r1, ...).

Usage :
    export NETBOX_TOKEN=nbt_xxx.yyy        (ou un token v1)
    python seed.py
"""
import os
import sys

import requests
import urllib3

urllib3.disable_warnings()

URL = os.environ.get("NETBOX_URL", "https://127.0.0.1").rstrip("/")
TOKEN = os.environ.get("NETBOX_TOKEN") or sys.exit("NETBOX_TOKEN n'est pas défini")
PREFIX = "clab-netauto-"

S = requests.Session()
S.verify = False  # certificat auto-signé du lab
S.headers.update({
    "Authorization": f"Bearer {TOKEN}" if TOKEN.startswith("nbt_") else f"Token {TOKEN}",
    "Accept": "application/json",
})

# ---------------------------------------------------------------- données
# Une seule description du réseau, poussée dans NetBox.
DEVICES = {
    "r1": {
        "mgmt": ("mgmt0", "172.20.20.11/24"),
        "interfaces": {"ethernet-1/1": "192.168.10.1/24", "ethernet-1/2": "10.0.12.1/30"},
        "context": {"asn": 65001, "router_id": "10.0.12.1",
                    "bgp_neighbors": [{"ip": "10.0.12.2", "peer_as": 65002}]},
    },
    "r2": {
        "mgmt": ("mgmt0", "172.20.20.12/24"),
        "interfaces": {"ethernet-1/1": "192.168.20.1/24", "ethernet-1/2": "10.0.12.2/30"},
        "context": {"asn": 65002, "router_id": "10.0.12.2",
                    "bgp_neighbors": [{"ip": "10.0.12.1", "peer_as": 65001}]},
    },
    "sw1": {
        "mgmt": ("mgmt0", "172.20.20.21/24"),
        "interfaces": {"ethernet-1/1": None, "ethernet-1/2": None},
        "context": {"mac_vrf": "lan-a"},
    },
    "sw2": {
        "mgmt": ("mgmt0", "172.20.20.22/24"),
        "interfaces": {"ethernet-1/1": None, "ethernet-1/2": None},
        "context": {"mac_vrf": "lan-b"},
    },
    "client1": {
        "mgmt": ("eth0", "172.20.20.31/24"),
        "interfaces": {"eth1": "192.168.10.10/24"},
        "context": None,
    },
    "client2": {
        "mgmt": ("eth0", "172.20.20.32/24"),
        "interfaces": {"eth1": "192.168.20.10/24"},
        "context": None,
    },
}

CABLES = [
    (("client1", "eth1"), ("sw1", "ethernet-1/1")),
    (("sw1", "ethernet-1/2"), ("r1", "ethernet-1/1")),
    (("r1", "ethernet-1/2"), ("r2", "ethernet-1/2")),
    (("r2", "ethernet-1/1"), ("sw2", "ethernet-1/2")),
    (("sw2", "ethernet-1/1"), ("client2", "eth1")),
]


# ---------------------------------------------------------------- API
def api(method, path, **kwargs):
    r = S.request(method, f"{URL}/api/{path}", **kwargs)
    if not r.ok:
        print(f"ERREUR {method} {path} : {r.status_code} {r.text}")
        r.raise_for_status()
    return r.json() if r.content else None


def get_one(path, **params):
    results = api("GET", path, params=params)["results"]
    return results[0] if results else None


def device(short):
    d = get_one("dcim/devices/", name=PREFIX + short)
    if not d:
        sys.exit(f"Équipement {PREFIX + short} introuvable dans NetBox")
    return d


def interface(dev, name, mgmt=False):
    i = get_one("dcim/interfaces/", device_id=dev["id"], name=name)
    if i:
        return i
    print(f"  + interface {dev['name']} {name}")
    return api("POST", "dcim/interfaces/", json={
        "device": dev["id"], "name": name, "mgmt_only": mgmt,
        "type": "1000base-t" if mgmt else "25gbase-x-sfp28",
    })


def ip_address(intf, address):
    ip = get_one("ipam/ip-addresses/", address=address)
    assigned = {"assigned_object_type": "dcim.interface", "assigned_object_id": intf["id"]}
    if ip is None:
        print(f"  + IP {address} -> {intf['name']}")
        return api("POST", "ipam/ip-addresses/", json={"address": address, "status": "active", **assigned})
    if ip.get("assigned_object_id") != intf["id"]:
        print(f"  ~ IP {address} rattachée à {intf['name']}")
        return api("PATCH", f"ipam/ip-addresses/{ip['id']}/", json=assigned)
    return ip


def cable(a, b):
    ia = get_one("dcim/interfaces/", device_id=device(a[0])["id"], name=a[1])
    ib = get_one("dcim/interfaces/", device_id=device(b[0])["id"], name=b[1])
    if ia["cable"] or ib["cable"]:
        return
    print(f"  + câble {a[0]}:{a[1]} <-> {b[0]}:{b[1]}")
    api("POST", "dcim/cables/", json={
        "a_terminations": [{"object_type": "dcim.interface", "object_id": ia["id"]}],
        "b_terminations": [{"object_type": "dcim.interface", "object_id": ib["id"]}],
        "status": "connected",
    })


# ---------------------------------------------------------------- main
def main():
    print(f"NetBox : {URL}")
    for short, spec in DEVICES.items():
        dev = device(short)
        print(f"{dev['name']}")
        mgmt_name, mgmt_ip = spec["mgmt"]
        mgmt_ipobj = ip_address(interface(dev, mgmt_name, mgmt=True), mgmt_ip)
        for name, address in spec["interfaces"].items():
            intf = interface(dev, name)
            if address:
                ip_address(intf, address)
        patch = {"primary_ip4": mgmt_ipobj["id"]}
        if spec["context"] is not None:
            patch["local_context_data"] = spec["context"]
        api("PATCH", f"dcim/devices/{dev['id']}/", json=patch)
        print("  = primary IP et données BGP / mac-vrf à jour")

    print("Câbles")
    for a, b in CABLES:
        cable(a, b)
    print("Terminé")


if __name__ == "__main__":
    main()

"""Inspection temps réel du lab : état BGP, état des interfaces et ping client <-> client.

Interroge directement les équipements SR Linux en JSON-RPC (HTTPS) avec les
identifiants du vault Ansible, et exécute `docker exec <client> ping` sans shell.
"""
from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
import urllib3

from webui.netbox_client import NetBoxClient, load_vault_secrets

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _srl_jsonrpc_get(
    mgmt_ip: str, username: str, password: str, paths: list[str], timeout: int = 5
) -> list[Any]:
    """Appelle la méthode JSON-RPC `get` (datastore state) d'un équipement SR Linux."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "get",
        "params": {
            "commands": [{"path": p, "datastore": "state"} for p in paths],
        },
    }
    resp = requests.post(
        f"https://{mgmt_ip}/jsonrpc",
        auth=(username, password),
        json=payload,
        verify=False,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"JSON-RPC error: {data['error']}")
    return data.get("result", [])


def _extract_scalar(value: Any) -> str:
    """Normalise une valeur retournée par SR Linux JSON-RPC (chaîne ou dict imbriqué)."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if len(value) == 1:
            return _extract_scalar(next(iter(value.values())))
        for key in ("oper-state", "admin-state", "session-state"):
            if key in value:
                return str(value[key])
    if isinstance(value, list) and value:
        return _extract_scalar(value[0])
    return "unknown"


def _inspect_single_device(
    node: dict[str, Any], username: str, password: str
) -> dict[str, Any]:
    """Interroge un routeur ou switch SR Linux et compare son état réel à NetBox."""
    mgmt_ip = (node.get("mgmt_ip") or "").split("/")[0]
    role = node.get("role", "unknown")
    data_intfs = [i for i in node.get("interfaces", []) if not i.get("mgmt_only", False)]
    expected_bgp = (node.get("context") or {}).get("bgp_neighbors") or []

    result: dict[str, Any] = {
        "id": node["id"],
        "role": role,
        "mgmt_ip": mgmt_ip,
        "reachable": False,
        "error": None,
        "interfaces": [],
        "bgp": [],
    }

    if not mgmt_ip:
        result["error"] = "Aucune IP de management"
        return result

    paths: list[str] = []
    for intf in data_intfs:
        paths.append(f"/interface[name={intf['name']}]/oper-state")
        paths.append(f"/interface[name={intf['name']}]/admin-state")

    bgp_start_idx = len(paths)
    if role == "router":
        for nb in expected_bgp:
            paths.append(
                f"/network-instance[name=default]/protocols/bgp/neighbor[peer-address={nb['ip']}]/session-state"
            )

    if not paths:
        result["reachable"] = True
        return result

    try:
        values = _srl_jsonrpc_get(mgmt_ip, username, password, paths)
    except Exception as exc:
        result["error"] = f"Injoignable ({exc.__class__.__name__})"
        return result

    result["reachable"] = True
    for i, intf in enumerate(data_intfs):
        oper = _extract_scalar(values[2 * i]) if 2 * i < len(values) else "unknown"
        admin = _extract_scalar(values[2 * i + 1]) if 2 * i + 1 < len(values) else "unknown"
        result["interfaces"].append({
            "name": intf["name"],
            "ips": intf.get("ips", []),
            "oper_state": oper,
            "admin_state": admin,
            "ok": oper.lower() == "up",
        })

    if role == "router":
        for j, nb in enumerate(expected_bgp):
            idx = bgp_start_idx + j
            state = _extract_scalar(values[idx]) if idx < len(values) else "unknown"
            result["bgp"].append({
                "peer_ip": nb["ip"],
                "peer_as": nb["peer_as"],
                "session_state": state,
                "ok": state.lower() == "established",
            })

    return result


def get_lab_live_status(lab_name: str, client: NetBoxClient | None = None) -> dict[str, Any]:
    """Récupère l'état temps réel (interfaces + BGP) de tous les équipements SR Linux du lab."""
    nb = client or NetBoxClient()
    topo = nb.get_lab_topology(lab_name)
    secrets = load_vault_secrets()
    username = secrets.get("vault_srl_user", "admin")
    password = secrets.get("vault_srl_password", "")

    srl_nodes = [n for n in topo.get("nodes", []) if n.get("role") in ("router", "switch")]
    devices: list[dict[str, Any]] = []

    if srl_nodes:
        with ThreadPoolExecutor(max_workers=min(8, len(srl_nodes))) as pool:
            futures = [pool.submit(_inspect_single_device, n, username, password) for n in srl_nodes]
            devices = [f.result() for f in futures]

    all_ok = bool(devices) and all(
        d["reachable"]
        and all(itf["ok"] for itf in d["interfaces"])
        and all(b["ok"] for b in d["bgp"])
        for d in devices
    )
    return {
        "lab": lab_name,
        "devices": devices,
        "all_ok": all_ok,
    }


def run_client_ping(
    lab_name: str,
    source_id: str,
    target_id: str,
    count: int = 3,
    client: NetBoxClient | None = None,
) -> dict[str, Any]:
    """Exécute un ping entre deux clients du lab via `docker exec` (sans shell)."""
    if source_id == target_id:
        raise ValueError("Les clients source et destination doivent être différents.")
    count = max(1, min(int(count), 10))

    nb = client or NetBoxClient()
    topo = nb.get_lab_topology(lab_name)
    clients = {n["id"]: n for n in topo.get("nodes", []) if n.get("role") == "container"}

    if source_id not in clients:
        raise ValueError(f"Client source '{source_id}' introuvable dans le lab '{lab_name}'.")
    if target_id not in clients:
        raise ValueError(f"Client destination '{target_id}' introuvable dans le lab '{lab_name}'.")

    src_container = clients[source_id]["device_name"]
    dst_intfs = [
        i for i in clients[target_id].get("interfaces", []) if not i.get("mgmt_only", False) and i.get("ips")
    ]
    if not dst_intfs:
        raise ValueError(f"Aucune IP de données trouvée sur le client '{target_id}'.")
    target_ip = dst_intfs[0]["ips"][0].split("/")[0]

    proc = subprocess.run(
        ["docker", "exec", src_container, "ping", "-c", str(count), "-W", "1", target_ip],
        capture_output=True,
        text=True,
        timeout=count * 2 + 5,
    )
    output = (proc.stdout + proc.stderr).strip()
    return {
        "lab": lab_name,
        "source": source_id,
        "target": target_id,
        "target_ip": target_ip,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "output": output,
    }

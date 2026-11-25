"""Générateur déterministe NetBox -> fichier Containerlab (<lab>.clab.yml).

Prend la topologie normalisée retournée par NetBoxClient.get_lab_topology()
et produit un fichier Containerlab reproductible octet pour octet.
"""
from __future__ import annotations

import difflib
import ipaddress
import re
from collections import deque
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SRLINUX_IMAGE = "ghcr.io/nokia/srlinux:latest"
DEFAULT_CLIENT_IMAGE = "ghcr.io/hellt/network-multitool"

# Ordre d'affichage des rôles dans la section nodes
ROLE_ORDER = {"router": 0, "switch": 1, "container": 2}

_SRL_INTF_RE = re.compile(r"^ethernet-(\d+)/(\d+)$")


def netbox_to_clab_interface(intf_name: str) -> str:
    """Convertit un nom d'interface NetBox/SR Linux vers la syntaxe Containerlab.

    Exemples :
        ethernet-1/1  -> e1-1
        ethernet-1/12 -> e1-12
        eth1          -> eth1
    """
    match = _SRL_INTF_RE.match(intf_name)
    if match:
        return f"e{match.group(1)}-{match.group(2)}"
    return intf_name


def _infer_mgmt_subnet(nodes: list[dict[str, Any]]) -> str:
    """Déduit le sous-réseau de management à partir des IP mgmt des nœuds."""
    for node in nodes:
        mgmt_ip = node.get("mgmt_ip")
        if mgmt_ip:
            net = ipaddress.ip_network(mgmt_ip, strict=False)
            return str(net)
    raise ValueError("Impossible de déduire le sous-réseau de management : aucune mgmt_ip définie.")


def _find_client_gateway(
    client_id: str,
    nodes_by_id: dict[str, dict[str, Any]],
    links: list[dict[str, Any]],
) -> str | None:
    """Trouve l'IP de passerelle (interface du routeur) d'un client via traversée L2.

    Parcourt les câbles depuis le client en traversant les switchs jusqu'à
    l'interface d'un routeur.
    """
    # Construire la liste d'adjacence avec les noms d'interfaces de chaque côté
    adj: dict[str, list[tuple[str, str]]] = {nid: [] for nid in nodes_by_id}
    for link in links:
        a, b = link["from"], link["to"]
        if a in adj and b in adj:
            adj[a].append((b, link["to_interface"]))
            adj[b].append((a, link["from_interface"]))

    visited = {client_id}
    queue: deque[str] = deque([client_id])

    while queue:
        current = queue.popleft()
        for neighbor_id, neighbor_intf_name in adj.get(current, []):
            if neighbor_id in visited:
                continue
            neighbor = nodes_by_id[neighbor_id]
            role = neighbor.get("role")
            if role == "router":
                for intf in neighbor.get("interfaces", []):
                    if intf["name"] == neighbor_intf_name and intf.get("ips"):
                        return intf["ips"][0].split("/")[0]
            elif role == "switch":
                visited.add(neighbor_id)
                queue.append(neighbor_id)

    return None


def _client_exec_commands(
    client: dict[str, Any],
    nodes_by_id: dict[str, dict[str, Any]],
    links: list[dict[str, Any]],
) -> list[str]:
    """Génère les commandes `exec` Containerlab pour un client Linux."""
    cmds: list[str] = []
    data_intfs = [i for i in client.get("interfaces", []) if not i.get("mgmt_only", False)]
    data_intf = next((i for i in data_intfs if i["name"] == "eth1"), data_intfs[0] if data_intfs else None)

    if data_intf and data_intf.get("ips"):
        ip_cidr = data_intf["ips"][0]
        intf_name = data_intf["name"]
        cmds.append(f"ip addr add {ip_cidr} dev {intf_name}")

        gw = _find_client_gateway(client["id"], nodes_by_id, links)
        if gw:
            cmds.append(f"ip route replace default via {gw} dev {intf_name}")

    return cmds


def generate_clab_dict(
    topology: dict[str, Any],
    *,
    srlinux_image: str = DEFAULT_SRLINUX_IMAGE,
    client_image: str = DEFAULT_CLIENT_IMAGE,
) -> dict[str, Any]:
    """Construit le dictionnaire Containerlab déterministe à partir d'une topologie NetBox."""
    lab_name = topology["name"]
    nodes_list = topology.get("nodes", [])
    links_list = topology.get("links", [])
    nodes_by_id = {n["id"]: n for n in nodes_list}

    mgmt_subnet = _infer_mgmt_subnet(nodes_list)

    # Trier les nœuds de façon déterministe : routeurs, switchs, clients, puis nom
    sorted_nodes = sorted(
        nodes_list,
        key=lambda n: (ROLE_ORDER.get(n.get("role", ""), 99), n["id"]),
    )

    clab_nodes: dict[str, Any] = {}
    for node in sorted_nodes:
        role = node.get("role")
        mgmt_ip = (node.get("mgmt_ip") or "").split("/")[0]
        if role in ("router", "switch"):
            node_cfg: dict[str, Any] = {"kind": "nokia_srlinux"}
            if mgmt_ip:
                node_cfg["mgmt-ipv4"] = mgmt_ip
            clab_nodes[node["id"]] = node_cfg
        elif role == "container":
            node_cfg = {"kind": "linux"}
            if mgmt_ip:
                node_cfg["mgmt-ipv4"] = mgmt_ip
            exec_cmds = _client_exec_commands(node, nodes_by_id, links_list)
            if exec_cmds:
                node_cfg["exec"] = exec_cmds
            clab_nodes[node["id"]] = node_cfg
        else:
            raise ValueError(f"Rôle non supporté pour Containerlab : {role} (nœud {node['id']})")

    # Normaliser et trier les liens de façon canonique pour garantir le déterminisme
    canonical_edges: list[tuple[str, str]] = []
    for link in links_list:
        ep_a = f"{link['from']}:{netbox_to_clab_interface(link['from_interface'])}"
        ep_b = f"{link['to']}:{netbox_to_clab_interface(link['to_interface'])}"
        pair = (ep_a, ep_b) if ep_a <= ep_b else (ep_b, ep_a)
        canonical_edges.append(pair)

    canonical_edges.sort()
    clab_links = [{"endpoints": [a, b]} for a, b in canonical_edges]

    return {
        "name": lab_name,
        "mgmt": {
            "network": f"{lab_name}-mgmt",
            "ipv4-subnet": mgmt_subnet,
        },
        "topology": {
            "kinds": {
                "nokia_srlinux": {"image": srlinux_image},
                "linux": {"image": client_image},
            },
            "nodes": clab_nodes,
            "links": clab_links,
        },
    }


def generate_clab_yaml(
    topology: dict[str, Any],
    *,
    srlinux_image: str = DEFAULT_SRLINUX_IMAGE,
    client_image: str = DEFAULT_CLIENT_IMAGE,
) -> str:
    """Sérialise la topologie Containerlab en YAML déterministe."""
    data = generate_clab_dict(
        topology,
        srlinux_image=srlinux_image,
        client_image=client_image,
    )
    header = f"# Fichier généré automatiquement par NetAuto Studio depuis NetBox (lab : {data['name']})\n"
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return header + body


def compute_yaml_diff(old_text: str, new_text: str, filename: str = "topology.clab.yml") -> str:
    """Calcule un diff unifié entre l'ancienne et la nouvelle version du fichier."""
    diff = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
    )
    return "".join(diff)


def write_clab_file(topology: dict[str, Any], labs_root: Path) -> tuple[Path, str, str]:
    """Écrit `labs/<lab>/<lab>.clab.yml` et retourne (chemin, contenu, diff)."""
    lab_name = topology["name"]
    lab_dir = labs_root / lab_name
    lab_dir.mkdir(parents=True, exist_ok=True)
    target = lab_dir / f"{lab_name}.clab.yml"

    old_text = target.read_text(encoding="utf-8") if target.is_file() else ""
    new_text = generate_clab_yaml(topology)
    diff_text = compute_yaml_diff(old_text, new_text, filename=target.name)

    if new_text != old_text:
        target.write_text(new_text, encoding="utf-8")

    return target, new_text, diff_text

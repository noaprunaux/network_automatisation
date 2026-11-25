"""Validation de topologie, allocation IPAM/ASN et dérivation BGP.

Module pur Python : toute la logique métier est testable en unitaire
sans dépendre d'un serveur NetBox réel (via l'interface IpamAllocator).
"""
from __future__ import annotations

import ipaddress
import re
from collections import deque
from typing import Any, Protocol

from pydantic import BaseModel, Field

LAB_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
NODE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
VALID_ROLES = {"router", "switch", "container"}
PROTECTED_DELETE_LABS = {"netauto", "tp-automatisation"}
RESERVED_MGMT_NET = ipaddress.ip_network("172.20.20.0/24")
MAX_NODES = 10


class TopologyValidationError(ValueError):
    """Erreur de validation métier d'une topologie."""


class NodeInput(BaseModel):
    """Définition d'un nœud envoyée par le front."""

    id: str
    role: str
    mgmt_ip: str | None = None
    asn: int | None = None
    router_id: str | None = None
    mac_vrf: str | None = None
    data_ip: str | None = None


class LinkInput(BaseModel):
    """Définition d'un lien entre deux nœuds."""

    from_node: str = Field(alias="from")
    to_node: str = Field(alias="to")

    model_config = {"populate_by_name": True}


class LabPayload(BaseModel):
    """Payload complet de création / modification d'un lab."""

    name: str
    nodes: list[NodeInput]
    links: list[LinkInput] = Field(default_factory=list)


class IpamAllocator(Protocol):
    """Interface d'allocation IPAM (implémentée par NetBoxClient ou un mock)."""

    def allocate_mgmt_prefix(self, lab_name: str) -> str:
        """Alloue un /24 libre pour le réseau de management du lab."""

    def allocate_p2p_prefix(self, lab_name: str, link_label: str) -> str:
        """Alloue un /30 libre pour un lien routeur <-> routeur."""

    def allocate_lan_prefix(self, lab_name: str, lan_label: str) -> str:
        """Alloue un /24 libre pour un segment LAN."""


def _validate_ipv4(value: str, field_name: str, node_id: str) -> ipaddress.IPv4Interface:
    """Valide une adresse IPv4 (avec ou sans préfixe)."""
    try:
        if "/" in value:
            return ipaddress.IPv4Interface(value)
        return ipaddress.IPv4Interface(f"{value}/24")
    except ValueError as exc:
        raise TopologyValidationError(
            f"Nœud '{node_id}' : {field_name} invalide ({value})."
        ) from exc


def validate_lab_payload(payload: LabPayload, *, allow_netauto: bool = False) -> None:
    """Valide strictement une topologie avant toute écriture dans NetBox."""
    name = payload.name.strip()
    if name in PROTECTED_DELETE_LABS and not (allow_netauto and name == "netauto"):
        raise TopologyValidationError(
            f"Le lab '{name}' ne peut pas être créé avec ce nom réservé."
        )
    if not LAB_NAME_RE.match(name):
        raise TopologyValidationError(
            f"Nom de lab invalide '{name}' : utilisez 2 à 16 caractères minuscules "
            "(lettre au début, puis lettres, chiffres ou tirets)."
        )

    if not payload.nodes:
        raise TopologyValidationError("Le lab doit contenir au moins un nœud.")
    if len(payload.nodes) > MAX_NODES:
        raise TopologyValidationError(
            f"Nombre maximum de nœuds dépassé ({len(payload.nodes)} > {MAX_NODES}) : "
            "chaque équipement SR Linux consomme ~1 à 1,5 Go de RAM."
        )

    nodes_by_id: dict[str, NodeInput] = {}
    used_asns: set[int] = set()
    used_mgmt_ips: set[str] = set()

    for node in payload.nodes:
        nid = node.id.strip()
        if not NODE_NAME_RE.match(nid):
            raise TopologyValidationError(
                f"Nom de nœud invalide '{nid}' : 1 à 16 caractères minuscules "
                "(lettre au début, lettres/chiffres/tirets)."
            )
        if nid in nodes_by_id:
            raise TopologyValidationError(f"Nom de nœud en doublon : '{nid}'.")
        if node.role not in VALID_ROLES:
            raise TopologyValidationError(
                f"Rôle inconnu '{node.role}' sur '{nid}' (attendu : router, switch ou container)."
            )

        if node.mgmt_ip:
            intf = _validate_ipv4(node.mgmt_ip, "IP de management", nid)
            if name != "netauto" and intf.ip in RESERVED_MGMT_NET:
                raise TopologyValidationError(
                    f"Nœud '{nid}' : le sous-réseau 172.20.20.0/24 est réservé au lab netauto."
                )
            ip_str = str(intf.ip)
            if ip_str in used_mgmt_ips:
                raise TopologyValidationError(f"IP de management en doublon : {ip_str}.")
            used_mgmt_ips.add(ip_str)

        if node.asn is not None:
            if node.role != "router":
                raise TopologyValidationError(f"Seul un routeur peut avoir un ASN (nœud '{nid}').")
            if not (1 <= node.asn <= 4294967295):
                raise TopologyValidationError(f"ASN invalide sur '{nid}' : {node.asn}.")
            if node.asn in used_asns:
                raise TopologyValidationError(f"ASN {node.asn} utilisé par plusieurs routeurs.")
            used_asns.add(node.asn)

        if node.router_id:
            if node.role != "router":
                raise TopologyValidationError(f"Seul un routeur peut avoir un router_id ('{nid}').")
            try:
                ipaddress.IPv4Address(node.router_id)
            except ValueError as exc:
                raise TopologyValidationError(
                    f"Router ID invalide sur '{nid}' : {node.router_id}."
                ) from exc

        if node.data_ip:
            _validate_ipv4(node.data_ip, "IP de données", nid)

        nodes_by_id[nid] = node

    if not any(n.role == "router" for n in payload.nodes):
        raise TopologyValidationError("Le lab doit contenir au moins un routeur.")

    # Validation des liens
    seen_pairs: set[frozenset[str]] = set()
    adj: dict[str, list[str]] = {nid: [] for nid in nodes_by_id}

    for link in payload.links:
        a, b = link.from_node.strip(), link.to_node.strip()
        if a not in nodes_by_id or b not in nodes_by_id:
            raise TopologyValidationError(f"Lien invalide ({a} <-> {b}) : nœud inexistant.")
        if a == b:
            raise TopologyValidationError(f"Un nœud ne peut pas être relié à lui-même ({a}).")
        pair = frozenset((a, b))
        if pair in seen_pairs:
            raise TopologyValidationError(f"Lien en doublon entre '{a}' et '{b}'.")
        seen_pairs.add(pair)

        ra, rb = nodes_by_id[a].role, nodes_by_id[b].role
        if ra == "container" and rb == "container":
            raise TopologyValidationError(
                f"Lien direct interdit entre deux clients ({a} <-> {b})."
            )
        adj[a].append(b)
        adj[b].append(a)

    # Chaque client doit avoir exactement 1 lien (vers un switch ou un routeur)
    for nid, node in nodes_by_id.items():
        if node.role == "container":
            if len(adj[nid]) != 1:
                raise TopologyValidationError(
                    f"Le client '{nid}' doit être relié à exactement un switch ou un routeur "
                    f"(actuellement {len(adj[nid])} lien(s))."
                )

    # Chaque segment L2 (composante connexe de switchs) ne peut toucher qu'un seul routeur
    visited_sw: set[str] = set()
    for nid, node in nodes_by_id.items():
        if node.role != "switch" or nid in visited_sw:
            continue
        queue = deque([nid])
        visited_sw.add(nid)
        connected_routers: set[str] = set()
        while queue:
            cur = queue.popleft()
            for nb in adj[cur]:
                nb_role = nodes_by_id[nb].role
                if nb_role == "router":
                    connected_routers.add(nb)
                elif nb_role == "switch" and nb not in visited_sw:
                    visited_sw.add(nb)
                    queue.append(nb)
        if len(connected_routers) > 1:
            raise TopologyValidationError(
                f"Segment L2 autour de '{nid}' relié à plusieurs routeurs "
                f"({', '.join(sorted(connected_routers))}) : un seul routeur par segment LAN est autorisé."
            )


def plan_lab_topology(
    payload: LabPayload,
    allocator: IpamAllocator,
    *,
    allow_netauto: bool = False,
) -> dict[str, Any]:
    """Valide la topologie et calcule l'allocation complète (IPAM, interfaces, ASN, BGP).

    Retourne un dictionnaire prêt à être écrit dans NetBox et passé au générateur Containerlab.
    """
    validate_lab_payload(payload, allow_netauto=allow_netauto)
    lab_name = payload.name.strip()

    nodes_by_id = {n.id.strip(): n for n in payload.nodes}
    role_order = {"router": 0, "switch": 1, "container": 2}
    ordered_ids = sorted(nodes_by_id.keys(), key=lambda nid: (role_order[nodes_by_id[nid].role], nid))

    # 1. Management /24 et IP mgmt par nœud
    mgmt_prefix = allocator.allocate_mgmt_prefix(lab_name)
    mgmt_net = ipaddress.IPv4Network(mgmt_prefix, strict=False)
    mgmt_hosts = [str(ip) for ip in mgmt_net.hosts()]
    # Réserver .1 pour le bridge Docker Containerlab, commencer les nœuds à .11
    mgmt_candidates = [ip for ip in mgmt_hosts if int(ip.split(".")[-1]) >= 11]
    forced_mgmt = {
        str(_validate_ipv4(n.mgmt_ip, "IP de management", n.id).ip)
        for n in payload.nodes
        if n.mgmt_ip
    }
    mgmt_pool_iter = iter([ip for ip in mgmt_candidates if ip not in forced_mgmt])

    mgmt_ip_by_node: dict[str, str] = {}
    for nid in ordered_ids:
        node = nodes_by_id[nid]
        if node.mgmt_ip:
            intf = _validate_ipv4(node.mgmt_ip, "IP de management", nid)
            mgmt_ip_by_node[nid] = str(intf)
        else:
            ip_str = next(mgmt_pool_iter)
            mgmt_ip_by_node[nid] = f"{ip_str}/{mgmt_net.prefixlen}"

    # 2. Interfaces & câbles (ordre déterministe)
    canonical_links = sorted(
        (
            (l.from_node.strip(), l.to_node.strip())
            if l.from_node.strip() <= l.to_node.strip()
            else (l.to_node.strip(), l.from_node.strip())
        )
        for l in payload.links
    )

    # Compteur d'interfaces par équipement SR Linux (ethernet-1/1, 1/2, ...)
    srl_port_idx: dict[str, int] = {nid: 0 for nid in ordered_ids}
    interfaces_by_node: dict[str, list[dict[str, Any]]] = {}
    for nid in ordered_ids:
        role = nodes_by_id[nid].role
        mgmt_intf_name = "eth0" if role == "container" else "mgmt0"
        interfaces_by_node[nid] = [
            {"name": mgmt_intf_name, "mgmt_only": True, "ips": [mgmt_ip_by_node[nid]]}
        ]

    def alloc_intf(nid: str) -> dict[str, Any]:
        role = nodes_by_id[nid].role
        if role == "container":
            name = "eth1"
        else:
            srl_port_idx[nid] += 1
            name = f"ethernet-1/{srl_port_idx[nid]}"
        entry = {"name": name, "mgmt_only": False, "ips": []}
        interfaces_by_node[nid].append(entry)
        return entry

    planned_links: list[dict[str, Any]] = []
    # Pour retrouver l'objet interface créé pour chaque extrémité d'un lien
    link_intf_map: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}

    for idx, (a, b) in enumerate(canonical_links, start=1):
        ia = alloc_intf(a)
        ib = alloc_intf(b)
        planned_links.append({
            "id": idx,
            "from": a,
            "from_interface": ia["name"],
            "to": b,
            "to_interface": ib["name"],
        })
        link_intf_map[(a, b)] = (ia, ib)

    # 3. Attribution des ASN (à partir de 65001)
    forced_asns = {n.asn for n in payload.nodes if n.asn is not None}
    next_asn = 65001
    asn_by_router: dict[str, int] = {}
    for nid in ordered_ids:
        node = nodes_by_id[nid]
        if node.role != "router":
            continue
        if node.asn is not None:
            asn_by_router[nid] = node.asn
        else:
            while next_asn in forced_asns:
                next_asn += 1
            asn_by_router[nid] = next_asn
            forced_asns.add(next_asn)
            next_asn += 1

    # 4. Liens routeur <-> routeur : /30 + voisins eBGP dérivés automatiquement
    bgp_neighbors: dict[str, list[dict[str, Any]]] = {r: [] for r in asn_by_router}
    first_p2p_ip: dict[str, str] = {}
    allocated_prefixes: list[dict[str, str]] = [{"prefix": mgmt_prefix, "role": "mgmt"}]

    for a, b in canonical_links:
        if nodes_by_id[a].role == "router" and nodes_by_id[b].role == "router":
            p2p_prefix = allocator.allocate_p2p_prefix(lab_name, f"{a}-{b}")
            allocated_prefixes.append({"prefix": p2p_prefix, "role": "p2p"})
            p2p_net = ipaddress.IPv4Network(p2p_prefix, strict=False)
            hosts = list(p2p_net.hosts())
            ip_a, ip_b = str(hosts[0]), str(hosts[1])

            ia, ib = link_intf_map[(a, b)]
            ia["ips"] = [f"{ip_a}/{p2p_net.prefixlen}"]
            ib["ips"] = [f"{ip_b}/{p2p_net.prefixlen}"]

            first_p2p_ip.setdefault(a, ip_a)
            first_p2p_ip.setdefault(b, ip_b)

            bgp_neighbors[a].append({"ip": ip_b, "peer_as": asn_by_router[b]})
            bgp_neighbors[b].append({"ip": ip_a, "peer_as": asn_by_router[a]})

    # 5. Segments LAN : routeur -> switch(s) -> clients, ou routeur -> client direct
    adj: dict[str, list[tuple[str, dict[str, Any], dict[str, Any]]]] = {nid: [] for nid in ordered_ids}
    for (a, b), (ia, ib) in link_intf_map.items():
        adj[a].append((b, ia, ib))
        adj[b].append((a, ib, ia))

    mac_vrf_by_switch: dict[str, str] = {}
    first_lan_ip: dict[str, str] = {}
    lan_counter = 0

    for nid in ordered_ids:
        if nodes_by_id[nid].role != "router":
            continue
        # Chaque interface du routeur reliée à un switch ou un client démarre un segment LAN
        for nb_id, router_intf, nb_intf in adj[nid]:
            nb_role = nodes_by_id[nb_id].role
            if nb_role == "router":
                continue

            lan_counter += 1
            lan_label = f"lan-{lan_counter}"
            lan_prefix = allocator.allocate_lan_prefix(lab_name, lan_label)
            allocated_prefixes.append({"prefix": lan_prefix, "role": "lan"})
            lan_net = ipaddress.IPv4Network(lan_prefix, strict=False)
            lan_hosts = [str(ip) for ip in lan_net.hosts()]
            gw_ip = lan_hosts[0]  # .1
            router_intf["ips"] = [f"{gw_ip}/{lan_net.prefixlen}"]
            first_lan_ip.setdefault(nid, gw_ip)

            # Collecter tous les switchs et clients de ce segment L2 par BFS
            seg_switches: list[str] = []
            seg_clients: list[tuple[str, dict[str, Any]]] = []
            visited = {nid, nb_id}
            queue: deque[tuple[str, dict[str, Any]]] = deque([(nb_id, nb_intf)])

            while queue:
                cur_id, cur_intf = queue.popleft()
                cur_role = nodes_by_id[cur_id].role
                if cur_role == "container":
                    seg_clients.append((cur_id, cur_intf))
                elif cur_role == "switch":
                    seg_switches.append(cur_id)
                    for next_id, _, next_intf in adj[cur_id]:
                        if next_id not in visited and nodes_by_id[next_id].role != "router":
                            visited.add(next_id)
                            queue.append((next_id, next_intf))

            for sw_id in sorted(seg_switches):
                mac_vrf_by_switch[sw_id] = nodes_by_id[sw_id].mac_vrf or lan_label

            # Attribuer .10, .11, ... aux clients du segment (sauf override manuel)
            client_candidates = [ip for ip in lan_hosts if int(ip.split(".")[-1]) >= 10]
            forced_client_ips = {
                str(_validate_ipv4(nodes_by_id[cid].data_ip, "IP de données", cid).ip)
                for cid, _ in seg_clients
                if nodes_by_id[cid].data_ip
            }
            client_ip_iter = iter([ip for ip in client_candidates if ip not in forced_client_ips])
            for cid, cintf in sorted(seg_clients, key=lambda x: x[0]):
                override = nodes_by_id[cid].data_ip
                if override:
                    cintf["ips"] = [str(_validate_ipv4(override, "IP de données", cid))]
                else:
                    cintf["ips"] = [f"{next(client_ip_iter)}/{lan_net.prefixlen}"]

    # Switchs isolés (sans routeur) : attribuer tout de même un mac-vrf par défaut
    for nid in ordered_ids:
        if nodes_by_id[nid].role == "switch" and nid not in mac_vrf_by_switch:
            lan_counter += 1
            mac_vrf_by_switch[nid] = nodes_by_id[nid].mac_vrf or f"lan-{lan_counter}"

    # 6. Assemblage final des nœuds
    planned_nodes: list[dict[str, Any]] = []
    for nid in ordered_ids:
        node = nodes_by_id[nid]
        context: dict[str, Any] = {}
        if node.role == "router":
            router_id = (
                node.router_id
                or first_p2p_ip.get(nid)
                or first_lan_ip.get(nid)
                or mgmt_ip_by_node[nid].split("/")[0]
            )
            context = {
                "asn": asn_by_router[nid],
                "router_id": router_id,
                "bgp_neighbors": sorted(bgp_neighbors[nid], key=lambda x: x["ip"]),
            }
        elif node.role == "switch":
            context = {"mac_vrf": mac_vrf_by_switch[nid]}

        planned_nodes.append({
            "id": nid,
            "device_name": f"clab-{lab_name}-{nid}",
            "role": node.role,
            "device_type": "network-multitool" if node.role == "container" else "sr-linux",
            "mgmt_ip": mgmt_ip_by_node[nid],
            "interfaces": interfaces_by_node[nid],
            "context": context,
        })

    return {
        "name": lab_name,
        "site_slug": lab_name,
        "display_name": lab_name,
        "readonly": False,
        "mgmt_prefix": mgmt_prefix,
        "prefixes": allocated_prefixes,
        "nodes": planned_nodes,
        "links": planned_links,
    }

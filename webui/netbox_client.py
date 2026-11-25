"""Client API NetBox pour NetAuto Studio.

Lit la topologie (sites, équipements, interfaces, IP, câbles, config context)
directement depuis NetBox (source de vérité unique).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import requests
import urllib3
import yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Chemins du dépôt
REPO_ROOT = Path(__file__).resolve().parent.parent
VAULT_FILE = REPO_ROOT / "ansible" / "inventory" / "group_vars" / "all" / "vault.yml"

# Lab historique protégé (lecture seule)
PROTECTED_LAB = "netauto"
PROTECTED_SITE_SLUG = "tp-automatisation"


class NetBoxError(RuntimeError):
    """Erreur lors d'un appel à l'API NetBox."""


class LabNotFoundError(NetBoxError):
    """Le lab demandé n'existe pas dans NetBox."""


def load_vault_secrets() -> dict[str, str]:
    """Lit les secrets du vault Ansible en mémoire (ne les journalise jamais)."""
    vault_pass_file = Path(
        os.environ.get("ANSIBLE_VAULT_PASSWORD_FILE", "~/.vault_pass")
    ).expanduser()
    if not vault_pass_file.is_file() or not VAULT_FILE.is_file():
        return {}

    venv_vault = Path(os.sys.executable).parent / "ansible-vault"
    vault_bin = str(venv_vault) if venv_vault.is_file() else "ansible-vault"
    proc = subprocess.run(
        [
            vault_bin,
            "view",
            "--vault-password-file",
            str(vault_pass_file),
            str(VAULT_FILE),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        return {}
    data = yaml.safe_load(proc.stdout) or {}
    return {k: str(v) for k, v in data.items() if v is not None}


def load_netbox_token() -> str:
    """Charge le token NetBox depuis l'environnement ou le vault Ansible.

    Ne journalise jamais le token.
    """
    token = os.environ.get("NETBOX_TOKEN", "").strip()
    if token:
        return token

    data = load_vault_secrets()
    token = data.get("vault_netbox_token", "").strip()
    if not token:
        raise NetBoxError(
            "NETBOX_TOKEN absent et impossible de lire vault_netbox_token depuis le vault Ansible."
        )
    return token


def lab_to_site_slug(lab_name: str) -> str:
    """Convertit un nom de lab en slug de site NetBox."""
    return PROTECTED_SITE_SLUG if lab_name == PROTECTED_LAB else lab_name


def site_slug_to_lab(site_slug: str) -> str:
    """Convertit un slug de site NetBox en nom de lab."""
    return PROTECTED_LAB if site_slug == PROTECTED_SITE_SLUG else site_slug


def short_node_name(device_name: str, lab_name: str) -> str:
    """Extrait le nom court d'un équipement `clab-<lab>-<nœud>`."""
    prefix = f"clab-{lab_name}-"
    if device_name.startswith(prefix):
        return device_name[len(prefix):]
    return device_name


class NetBoxClient:
    """Client léger sur l'API REST NetBox 4.x (tokens v1 et v2)."""

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        verify_ssl: bool = False,
        timeout: int = 10,
    ) -> None:
        self.url = (url or os.environ.get("NETBOX_URL", "https://127.0.0.1")).rstrip("/")
        self.timeout = timeout
        tok = token if token is not None else load_netbox_token()

        self.session = requests.Session()
        self.session.verify = verify_ssl
        auth = f"Bearer {tok}" if tok.startswith("nbt_") else f"Token {tok}"
        self.session.headers.update({
            "Authorization": auth,
            "Accept": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = path if path.startswith("http") else f"{self.url}/api/{path.lstrip('/')}"
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise NetBoxError(f"NetBox injoignable : {exc}") from exc

        if not resp.ok:
            raise NetBoxError(f"Erreur NetBox {resp.status_code} sur {path}")
        return resp.json() if resp.content else None

    def get_all(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """Récupère tous les résultats paginés d'un endpoint NetBox."""
        query = {"limit": 200, **params}
        results: list[dict[str, Any]] = []
        data = self._request("GET", path, params=query)
        while data:
            results.extend(data.get("results", []))
            next_url = data.get("next")
            data = self._request("GET", next_url) if next_url else None
        return results

    def list_labs(self) -> list[dict[str, Any]]:
        """Retourne la liste des labs (un lab = un site NetBox)."""
        sites = self.get_all("dcim/sites/")
        devices = self.get_all("dcim/devices/")

        counts: dict[str, int] = {}
        for dev in devices:
            site = dev.get("site") or {}
            slug = site.get("slug")
            if slug:
                counts[slug] = counts.get(slug, 0) + 1

        labs: list[dict[str, Any]] = []
        for site in sites:
            slug = site["slug"]
            lab_name = site_slug_to_lab(slug)
            labs.append({
                "name": lab_name,
                "site_slug": slug,
                "display_name": site.get("name", lab_name),
                "readonly": False,
                "deletable": lab_name != PROTECTED_LAB,
                "device_count": counts.get(slug, 0),
            })

        # Placer netauto en premier pour la lisibilité
        labs.sort(key=lambda item: (0 if item["name"] == PROTECTED_LAB else 1, item["name"]))
        return labs

    def get_lab_topology(self, lab_name: str) -> dict[str, Any]:
        """Lit la topologie complète d'un lab depuis NetBox."""
        site_slug = lab_to_site_slug(lab_name)
        sites = self.get_all("dcim/sites/", slug=site_slug)
        if not sites:
            raise LabNotFoundError(f"Lab '{lab_name}' introuvable dans NetBox.")
        site = sites[0]

        devices = self.get_all("dcim/devices/", site=site_slug)
        interfaces = self.get_all("dcim/interfaces/", site=site_slug)
        ip_addresses = self.get_all("ipam/ip-addresses/")

        # Indexer les IP par interface_id
        ips_by_intf: dict[int, list[str]] = {}
        for ip_obj in ip_addresses:
            if ip_obj.get("assigned_object_type") == "dcim.interface":
                intf_id = ip_obj.get("assigned_object_id")
                if intf_id is not None:
                    ips_by_intf.setdefault(intf_id, []).append(ip_obj["address"])

        # Indexer les interfaces par device_id
        intfs_by_dev: dict[int, list[dict[str, Any]]] = {}
        for intf in interfaces:
            dev_id = (intf.get("device") or {}).get("id")
            if dev_id is not None:
                intfs_by_dev.setdefault(dev_id, []).append({
                    "id": intf["id"],
                    "name": intf["name"],
                    "mgmt_only": bool(intf.get("mgmt_only", False)),
                    "ips": sorted(ips_by_intf.get(intf["id"], [])),
                })

        # Construire les nœuds
        device_name_to_short: dict[str, str] = {}
        nodes: list[dict[str, Any]] = []
        for dev in sorted(devices, key=lambda d: d["name"]):
            short = short_node_name(dev["name"], lab_name)
            device_name_to_short[dev["name"]] = short
            dev_intfs = sorted(
                intfs_by_dev.get(dev["id"], []),
                key=lambda i: (0 if i["mgmt_only"] else 1, i["name"]),
            )
            primary_ip = (dev.get("primary_ip4") or {}).get("address")
            nodes.append({
                "id": short,
                "device_name": dev["name"],
                "role": (dev.get("role") or {}).get("slug", "unknown"),
                "device_type": (dev.get("device_type") or {}).get("slug", ""),
                "mgmt_ip": primary_ip,
                "interfaces": dev_intfs,
                "context": dev.get("local_context_data") or {},
            })

        # Construire les liens à partir des câbles reliant deux équipements de ce site
        cables = self.get_all("dcim/cables/")
        links: list[dict[str, Any]] = []
        for cab in sorted(cables, key=lambda c: c["id"]):
            a_terms = cab.get("a_terminations") or []
            b_terms = cab.get("b_terminations") or []
            if not a_terms or not b_terms:
                continue
            a_obj = a_terms[0].get("object") or {}
            b_obj = b_terms[0].get("object") or {}
            a_dev = (a_obj.get("device") or {}).get("name")
            b_dev = (b_obj.get("device") or {}).get("name")
            if a_dev in device_name_to_short and b_dev in device_name_to_short:
                links.append({
                    "id": cab["id"],
                    "from": device_name_to_short[a_dev],
                    "from_interface": a_obj.get("name", ""),
                    "to": device_name_to_short[b_dev],
                    "to_interface": b_obj.get("name", ""),
                })

        return {
            "name": lab_name,
            "site_slug": site_slug,
            "display_name": site.get("name", lab_name),
            "readonly": False,
            "deletable": lab_name != PROTECTED_LAB,
            "nodes": nodes,
            "links": links,
        }

    # ---------------------------------------------------------------- IPAM & écriture
    def _get_one(self, path: str, **params: Any) -> dict[str, Any] | None:
        results = self.get_all(path, **params)
        return results[0] if results else None

    def _ensure_prefix(self, prefix: str, status: str, description: str) -> dict[str, Any]:
        existing = self._get_one("ipam/prefixes/", prefix=prefix)
        if existing:
            return existing
        return self._request(
            "POST",
            "ipam/prefixes/",
            json={"prefix": prefix, "status": status, "description": description},
        )

    def ensure_ipam_pools(self) -> dict[str, int]:
        """Crée de façon idempotente les pools IPAM parents et préserve les préfixes de netauto."""
        mgmt_pool = self._ensure_prefix(
            "172.20.32.0/19", "container", "Pool management labs NetAuto Studio"
        )
        p2p_pool = self._ensure_prefix(
            "10.100.0.0/16", "container", "Pool liens P2P NetAuto Studio"
        )
        lan_pool = self._ensure_prefix(
            "192.168.0.0/16", "container", "Pool LAN NetAuto Studio"
        )
        # Préserver les préfixes historiques du lab netauto
        self._ensure_prefix("172.20.20.0/24", "active", "mgmt:netauto")
        self._ensure_prefix("10.0.12.0/30", "active", "p2p:netauto:r1-r2")
        self._ensure_prefix("192.168.10.0/24", "active", "lan:netauto:lan-1")
        self._ensure_prefix("192.168.20.0/24", "active", "lan:netauto:lan-2")
        return {
            "mgmt": mgmt_pool["id"],
            "p2p": p2p_pool["id"],
            "lan": lan_pool["id"],
        }

    def _allocate_from_pool(self, pool_key: str, prefix_length: int, description: str) -> str:
        # S'assurer que les pools et préfixes historiques existent avant recherche
        pools = self.ensure_ipam_pools()
        existing = self._get_one("ipam/prefixes/", description=description)
        if existing:
            return existing["prefix"]
        pool_id = pools[pool_key]
        created = self._request(
            "POST",
            f"ipam/prefixes/{pool_id}/available-prefixes/",
            json={"prefix_length": prefix_length, "status": "active", "description": description},
        )
        return created["prefix"]

    def allocate_mgmt_prefix(self, lab_name: str) -> str:
        if lab_name == PROTECTED_LAB:
            self.ensure_ipam_pools()
            return "172.20.20.0/24"
        return self._allocate_from_pool("mgmt", 24, f"mgmt:{lab_name}")

    def allocate_p2p_prefix(self, lab_name: str, link_label: str) -> str:
        return self._allocate_from_pool("p2p", 30, f"p2p:{lab_name}:{link_label}")

    def allocate_lan_prefix(self, lab_name: str, lan_label: str) -> str:
        return self._allocate_from_pool("lan", 24, f"lan:{lab_name}:{lan_label}")

    def _ensure_site(self, lab_name: str) -> dict[str, Any]:
        site = self._get_one("dcim/sites/", slug=lab_name)
        if site:
            return site
        return self._request(
            "POST",
            "dcim/sites/",
            json={"name": lab_name, "slug": lab_name, "status": "active"},
        )

    def _ensure_device(
        self, name: str, site_id: int, role_id: int, device_type_id: int
    ) -> dict[str, Any]:
        dev = self._get_one("dcim/devices/", name=name)
        if dev:
            return dev
        return self._request(
            "POST",
            "dcim/devices/",
            json={
                "name": name,
                "site": site_id,
                "role": role_id,
                "device_type": device_type_id,
                "status": "active",
            },
        )

    def _ensure_interface(self, device_id: int, name: str, mgmt_only: bool) -> dict[str, Any]:
        intf = self._get_one("dcim/interfaces/", device_id=device_id, name=name)
        if intf:
            return intf
        return self._request(
            "POST",
            "dcim/interfaces/",
            json={
                "device": device_id,
                "name": name,
                "mgmt_only": mgmt_only,
                "type": "1000base-t" if mgmt_only else "25gbase-x-sfp28",
            },
        )

    def _ensure_ip_on_interface(self, intf_id: int, address: str) -> dict[str, Any]:
        assigned = {"assigned_object_type": "dcim.interface", "assigned_object_id": intf_id}
        existing = self._get_one("ipam/ip-addresses/", address=address)
        if existing is None:
            return self._request(
                "POST",
                "ipam/ip-addresses/",
                json={"address": address, "status": "active", **assigned},
            )
        if existing.get("assigned_object_id") != intf_id:
            return self._request(
                "PATCH",
                f"ipam/ip-addresses/{existing['id']}/",
                json=assigned,
            )
        return existing

    def save_lab_topology(self, planned: dict[str, Any]) -> dict[str, Any]:
        """Enregistre de façon idempotente une topologie planifiée dans NetBox."""
        lab_name = planned["name"]
        site_slug = lab_to_site_slug(lab_name)

        site = self._ensure_site(site_slug)
        roles = {r["slug"]: r["id"] for r in self.get_all("dcim/device-roles/")}
        types = {t["slug"]: t["id"] for t in self.get_all("dcim/device-types/")}

        # 1. Supprimer d'abord les anciens câbles du site (évite les conflits de ports lors de la mise à jour)
        existing_devs = self.get_all("dcim/devices/", site=site_slug)
        existing_dev_names = {d["name"] for d in existing_devs}
        if existing_dev_names:
            for cab in self.get_all("dcim/cables/"):
                a_terms = cab.get("a_terminations") or []
                b_terms = cab.get("b_terminations") or []
                if not a_terms or not b_terms:
                    continue
                a_dev = ((a_terms[0].get("object") or {}).get("device") or {}).get("name")
                b_dev = ((b_terms[0].get("object") or {}).get("device") or {}).get("name")
                if a_dev in existing_dev_names or b_dev in existing_dev_names:
                    self._request("DELETE", f"dcim/cables/{cab['id']}/")

        # 2. Créer / mettre à jour équipements, interfaces, IP et local_context_data
        desired_dev_names: set[str] = set()
        intf_id_map: dict[tuple[str, str], int] = {}

        for node in planned["nodes"]:
            dev_name = node["device_name"]
            desired_dev_names.add(dev_name)
            role_id = roles.get(node["role"])
            type_id = types.get(node["device_type"])
            if not role_id or not type_id:
                raise NetBoxError(
                    f"Rôle '{node['role']}' ou type '{node['device_type']}' introuvable dans NetBox."
                )

            dev = self._ensure_device(dev_name, site["id"], role_id, type_id)
            desired_intf_names: set[str] = set()
            mgmt_ip_id: int | None = None

            for intf_spec in node["interfaces"]:
                desired_intf_names.add(intf_spec["name"])
                intf = self._ensure_interface(dev["id"], intf_spec["name"], intf_spec["mgmt_only"])
                intf_id_map[(node["id"], intf_spec["name"])] = intf["id"]

                # Nettoyer les anciennes IP sur cette interface qui ne correspondent plus
                desired_ips = set(intf_spec.get("ips", []))
                current_ips = self.get_all("ipam/ip-addresses/", interface_id=intf["id"])
                for cur_ip in current_ips:
                    if cur_ip["address"] not in desired_ips:
                        self._request("DELETE", f"ipam/ip-addresses/{cur_ip['id']}/")

                for addr in intf_spec.get("ips", []):
                    ip_obj = self._ensure_ip_on_interface(intf["id"], addr)
                    if intf_spec["mgmt_only"]:
                        mgmt_ip_id = ip_obj["id"]

            # Mettre à jour primary_ip4 et local_context_data
            patch: dict[str, Any] = {"local_context_data": node.get("context") or None}
            if mgmt_ip_id is not None:
                patch["primary_ip4"] = mgmt_ip_id
            self._request("PATCH", f"dcim/devices/{dev['id']}/", json=patch)

            # Supprimer les interfaces obsolètes sur cet équipement
            for cur_intf in self.get_all("dcim/interfaces/", device_id=dev["id"]):
                if cur_intf["name"] not in desired_intf_names:
                    for ip_obj in self.get_all("ipam/ip-addresses/", interface_id=cur_intf["id"]):
                        self._request("DELETE", f"ipam/ip-addresses/{ip_obj['id']}/")
                    self._request("DELETE", f"dcim/interfaces/{cur_intf['id']}/")

        # 3. Supprimer les équipements retirés du lab
        for old_dev in existing_devs:
            if old_dev["name"] not in desired_dev_names:
                for cur_intf in self.get_all("dcim/interfaces/", device_id=old_dev["id"]):
                    for ip_obj in self.get_all("ipam/ip-addresses/", interface_id=cur_intf["id"]):
                        self._request("DELETE", f"ipam/ip-addresses/{ip_obj['id']}/")
                self._request("DELETE", f"dcim/devices/{old_dev['id']}/")

        # 4. Créer les câbles
        for link in planned["links"]:
            ia_id = intf_id_map[(link["from"], link["from_interface"])]
            ib_id = intf_id_map[(link["to"], link["to_interface"])]
            self._request(
                "POST",
                "dcim/cables/",
                json={
                    "a_terminations": [{"object_type": "dcim.interface", "object_id": ia_id}],
                    "b_terminations": [{"object_type": "dcim.interface", "object_id": ib_id}],
                    "status": "connected",
                },
            )

        return self.get_lab_topology(lab_name)

    def delete_lab(self, lab_name: str) -> None:
        """Supprime définitivement un lab et tous ses objets dans NetBox.

        Interdit sur le lab historique netauto.
        """
        if lab_name in (PROTECTED_LAB, PROTECTED_SITE_SLUG):
            raise NetBoxError(f"Suppression interdite sur le lab protégé '{lab_name}'.")

        site = self._get_one("dcim/sites/", slug=lab_name)
        if not site:
            raise LabNotFoundError(f"Lab '{lab_name}' introuvable dans NetBox.")

        devs = self.get_all("dcim/devices/", site=lab_name)
        dev_names = {d["name"] for d in devs}

        # 1. Supprimer les câbles liés aux équipements du site
        if dev_names:
            for cab in self.get_all("dcim/cables/"):
                a_terms = cab.get("a_terminations") or []
                b_terms = cab.get("b_terminations") or []
                if not a_terms or not b_terms:
                    continue
                a_dev = ((a_terms[0].get("object") or {}).get("device") or {}).get("name")
                b_dev = ((b_terms[0].get("object") or {}).get("device") or {}).get("name")
                if a_dev in dev_names or b_dev in dev_names:
                    self._request("DELETE", f"dcim/cables/{cab['id']}/")

        # 2. Supprimer les IP puis les équipements du site
        for dev in devs:
            for intf in self.get_all("dcim/interfaces/", device_id=dev["id"]):
                for ip_obj in self.get_all("ipam/ip-addresses/", interface_id=intf["id"]):
                    self._request("DELETE", f"ipam/ip-addresses/{ip_obj['id']}/")
            self._request("DELETE", f"dcim/devices/{dev['id']}/")

        # 3. Libérer les préfixes IPAM alloués à ce lab
        prefixes = self.get_all("ipam/prefixes/")
        for pref in prefixes:
            desc = pref.get("description") or ""
            if desc == f"mgmt:{lab_name}" or desc.startswith(f"p2p:{lab_name}:") or desc.startswith(f"lan:{lab_name}:"):
                self._request("DELETE", f"ipam/prefixes/{pref['id']}/")

        # 4. Supprimer le site NetBox
        self._request("DELETE", f"dcim/sites/{site['id']}/")

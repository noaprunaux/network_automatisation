"""Fonctions communes aux scripts Nornir.

Nornir n'a plus d'inventaire à lui : il réutilise EXACTEMENT l'inventaire
d'Ansible (NetBox par défaut, via ansible.cfg), obtenu avec
`ansible-inventory --list`. Une seule source de vérité pour toute la chaîne :
- équipements, IP de management, rôles        -> NetBox
- interfaces et voisins BGP attendus          -> NetBox
- identifiants SR Linux                       -> vault Ansible (déchiffré par ansible-inventory)

Prérequis dans l'environnement : ANSIBLE_VAULT_PASSWORD_FILE et NETBOX_TOKEN.
Variable optionnelle NORNIR_INVENTORY : chemin d'un autre inventaire Ansible
(ex. ../ansible/inventory/hosts.yml, ou l'inventaire d'un autre lab).
"""
import json
import os
import subprocess
from pathlib import Path

from nornir import InitNornir
from nornir.core.inventory import (
    ConnectionOptions, Defaults, Group, Groups, Host, Hosts, Inventory, ParentGroups,
)
from nornir.core.plugins.inventory import InventoryPluginRegister

BASE = Path(__file__).resolve().parent
ANSIBLE_DIR = BASE.parent / "ansible"
ROLES = ("router", "switch")  # équipements gérés (les clients ne sont pas des SR Linux)
VAULT_FILE = ANSIBLE_DIR / "inventory" / "group_vars" / "all" / "vault.yml"


def unwrap(value):
    """`ansible-inventory --list` emballe les valeurs externes (NetBox) :
    {"__ansible_unsafe": "172.20.20.11"}. On récupère la valeur simple, récursivement."""
    if isinstance(value, dict):
        if set(value) == {"__ansible_unsafe"}:
            return unwrap(value["__ansible_unsafe"])
        return {k: unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap(v) for v in value]
    return value


def load_vault() -> dict:
    """Secours : lit les identifiants directement dans le vault."""
    import yaml
    out = subprocess.run(["ansible-vault", "view", str(VAULT_FILE)],
                         capture_output=True, text=True, check=True).stdout
    return yaml.safe_load(out)


class AnsibleInventory:
    """Plugin d'inventaire Nornir construit à partir de `ansible-inventory --list`."""

    def __init__(self, source: str | None = None, **_kwargs) -> None:
        self.source = source or os.environ.get("NORNIR_INVENTORY")

    def _ansible_list(self) -> dict:
        cmd = ["ansible-inventory", "--list"]
        if self.source:
            cmd += ["-i", str(Path(self.source).resolve())]
        out = subprocess.run(cmd, cwd=ANSIBLE_DIR, capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"ansible-inventory a échoué :\n{out.stderr}")
        return json.loads(out.stdout)

    def load(self) -> Inventory:
        data = unwrap(self._ansible_list())
        hostvars = data["_meta"]["hostvars"]

        srlinux = Group(
            name="srlinux",
            connection_options={
                # Netmiko : SSH + CLI
                "netmiko": ConnectionOptions(platform="nokia_srl"),
                # NAPALM : getters normalisés (driver napalm-srlinux, JSON-RPC)
                "napalm": ConnectionOptions(
                    platform="srlinux",
                    extras={"optional_args": {"jsonrpc_port": 443, "insecure": False,
                                              "skip_verify": True}},
                ),
            },
        )

        hosts = Hosts()
        for role in ROLES:
            for name in data.get(role, {}).get("hosts", []):
                hv = hostvars[name]
                hosts[name] = Host(
                    name=name,
                    hostname=hv.get("ansible_host", name),
                    username=hv.get("vault_srl_user"),
                    password=hv.get("vault_srl_password"),
                    groups=ParentGroups([srlinux]),
                    data={
                        "role": role,
                        "interfaces": hv.get("interfaces", []),
                        "bgp_neighbors": hv.get("bgp_neighbors", []),
                    },
                )
        return Inventory(hosts=hosts, groups=Groups({"srlinux": srlinux}), defaults=Defaults())


InventoryPluginRegister.register("AnsibleInventory", AnsibleInventory)


def expected_interfaces(host: Host) -> list[str]:
    """Interfaces de données attendues (hors management)."""
    return [i["name"] for i in host.data["interfaces"] if not i.get("mgmt_only", False)]


def expected_bgp_neighbors(host: Host) -> list[dict]:
    """Voisins BGP attendus : [{'ip': ..., 'peer_as': ...}]."""
    return host.data["bgp_neighbors"] or []


def get_nornir():
    """Initialise Nornir sur l'inventaire Ansible (NetBox par défaut)."""
    nr = InitNornir(config_file=str(BASE / "config.yaml"))
    hosts = list(nr.inventory.hosts.values())
    # Si ansible-inventory n'a pas fourni des identifiants en clair, on lit le vault.
    if any(not isinstance(h.username, str) or not isinstance(h.password, str) for h in hosts):
        creds = load_vault()
        for h in hosts:
            h.username = creds["vault_srl_user"]
            h.password = creds["vault_srl_password"]
    return nr

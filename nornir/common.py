"""Fonctions communes aux scripts Nornir.

- Les identifiants sont lus dans le vault Ansible (une seule source pour les secrets).
- Les valeurs attendues (interfaces, voisins BGP) sont lues dans les host_vars
  Ansible (une seule source de vérité pour les données réseau).
"""
import subprocess
from pathlib import Path

import yaml
from nornir import InitNornir

BASE = Path(__file__).resolve().parent
ANSIBLE_INV = BASE.parent / "ansible" / "inventory"
VAULT_FILE = ANSIBLE_INV / "group_vars" / "all" / "vault.yml"
HOST_VARS = ANSIBLE_INV / "host_vars"


def load_vault() -> dict:
    """Déchiffre le vault Ansible (nécessite ANSIBLE_VAULT_PASSWORD_FILE)."""
    out = subprocess.run(
        ["ansible-vault", "view", str(VAULT_FILE)],
        capture_output=True, text=True, check=True,
    ).stdout
    return yaml.safe_load(out)


def expected(host: str) -> dict:
    """Retourne l'état attendu d'un équipement depuis ses host_vars Ansible."""
    with open(HOST_VARS / f"{host}.yml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def expected_interfaces(host: str) -> list[str]:
    """Liste des interfaces attendues (format routeur ou switch)."""
    itfs = expected(host).get("interfaces", [])
    return [i["name"] if isinstance(i, dict) else i for i in itfs]


def get_nornir():
    """Initialise Nornir avec les identifiants du vault."""
    nr = InitNornir(config_file=str(BASE / "config.yaml"))
    creds = load_vault()
    nr.inventory.defaults.username = creds["vault_srl_user"]
    nr.inventory.defaults.password = creds["vault_srl_password"]
    return nr

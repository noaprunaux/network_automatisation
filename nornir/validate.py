"""Validation de l'état réel du réseau par rapport à l'état attendu.

Nornir (orchestration) + NAPALM (getters normalisés, indépendants du constructeur).
L'état attendu est lu dans les host_vars Ansible.

Usage : python validate.py   -> code retour 0 si tout est conforme, 1 sinon
"""
import sys

from nornir.core.task import Result, Task
from nornir_napalm.plugins.tasks import napalm_get
from nornir_utils.plugins.functions import print_result

from common import expected, expected_interfaces, get_nornir


def check_device(task: Task) -> Result:
    host = task.host.name
    getters = ["interfaces"]
    if task.host.data.get("role") == "router":
        getters.append("bgp_neighbors")

    data = task.run(task=napalm_get, getters=getters).result
    ok, errors = [], []

    # 1. Interfaces attendues opérationnelles
    for itf in expected_interfaces(host):
        state = data["interfaces"].get(itf)
        if state and state.get("is_up"):
            ok.append(f"interface {itf} up")
        else:
            errors.append(f"interface {itf} DOWN ou absente")

    # 2. Sessions BGP établies (routeurs uniquement)
    if "bgp_neighbors" in getters:
        peers = data["bgp_neighbors"].get("global", {}).get("peers", {})
        for n in expected(host).get("bgp_neighbors", []):
            peer = peers.get(n["ip"])
            if peer and peer.get("is_up"):
                ok.append(f"BGP {n['ip']} (AS {n['peer_as']}) établi")
            else:
                errors.append(f"BGP {n['ip']} (AS {n['peer_as']}) NON établi")

    report = "\n".join([f"  OK   {m}" for m in ok] + [f"  KO   {m}" for m in errors])
    if errors:
        # Lever une exception marque la tâche en échec dans Nornir
        raise RuntimeError(f"{host} non conforme :\n{report}")
    return Result(host=task.host, result=f"{host} conforme :\n{report}")


def main() -> int:
    nr = get_nornir()
    result = nr.run(task=check_device, name="Validation de l'état du réseau")
    print_result(result)
    if result.failed:
        print(f"\nÉCHEC : équipements non conformes -> {', '.join(result.failed_hosts)}")
        return 1
    print("\nSUCCÈS : tous les équipements sont conformes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

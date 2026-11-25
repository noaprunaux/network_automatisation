"""Sauvegarde de la configuration réelle des équipements.

Nornir (orchestration en parallèle) + Netmiko (connexion SSH, commande CLI).
Usage : python backup.py   -> fichiers dans nornir/backups/<host>.cfg
"""
import sys
from datetime import datetime

from nornir.core.task import Result, Task
from nornir_netmiko.tasks import netmiko_send_command
from nornir_utils.plugins.functions import print_result

from common import BASE, get_nornir

BACKUP_DIR = BASE / "backups"


def backup(task: Task) -> Result:
    r = task.run(task=netmiko_send_command, command_string="info flat")
    path = BACKUP_DIR / f"{task.host.name}.cfg"
    header = f"# Sauvegarde {task.host.name} - {datetime.now():%Y-%m-%d %H:%M:%S}\n"
    path.write_text(header + r.result + "\n", encoding="utf-8")
    return Result(host=task.host, result=f"Config sauvegardée dans {path.name}")


def main() -> int:
    BACKUP_DIR.mkdir(exist_ok=True)
    nr = get_nornir()
    result = nr.run(task=backup, name="Sauvegarde des configurations")
    print_result(result)
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())

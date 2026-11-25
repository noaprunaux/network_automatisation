"""Orchestrateur asynchrone des déploiements (Containerlab + Ansible + SSE).

Gère :
- le verrou exclusif fichier (/tmp/netauto.lock) via fcntl
- le masquage automatique des secrets dans tous les logs
- le streaming temps réel vers le front via Server-Sent Events (SSE)
- la persistance des journaux dans labs/<lab>/logs/
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import requests
import urllib3
import yaml

from webui.generator import compute_yaml_diff, generate_clab_dict, generate_clab_yaml
from webui.netbox_client import (
    PROTECTED_LAB,
    REPO_ROOT,
    NetBoxClient,
    load_netbox_token,
    load_vault_secrets,
)
from webui.topology import LabPayload, plan_lab_topology

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOCK_PATH = Path("/tmp/netauto.lock")
LABS_ROOT = REPO_ROOT / "labs"
ANSIBLE_DIR = REPO_ROOT / "ansible"
VENV_BIN = Path(os.sys.executable).parent


class OperationInProgressError(RuntimeError):
    """Une autre opération de déploiement détient déjà le verrou."""


@dataclass
class StepStatus:
    name: str
    status: str = "pending"  # pending | running | ok | skipped | error
    duration: float = 0.0
    message: str = ""


@dataclass
class Job:
    id: str
    lab: str
    mode: str  # "apply" | "preview"
    status: str = "running"  # running | ok | error
    started_at: float = field(default_factory=time.monotonic)
    duration: float = 0.0
    steps: list[StepStatus] = field(default_factory=list)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    subscribers: list[asyncio.Queue[tuple[str, dict[str, Any]] | None]] = field(default_factory=list)
    log_file: str = ""
    _secrets: list[str] = field(default_factory=list)
    _log_lines: list[str] = field(default_factory=list)

    def _mask(self, text: str) -> str:
        masked = text
        for secret in self._secrets:
            if secret and len(secret) >= 4:
                masked = masked.replace(secret, "***")
        return masked

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))
        for q in list(self.subscribers):
            q.put_nowait((event_type, payload))

    def log(self, line: str) -> None:
        clean = self._mask(line.rstrip("\r\n"))
        self._log_lines.append(clean)
        self.emit("log", {"line": clean})

    def update_step(self, idx: int, status: str, duration: float = 0.0, message: str = "") -> None:
        step = self.steps[idx]
        step.status = status
        step.duration = round(duration, 2)
        step.message = message
        self.emit("step", {
            "index": idx,
            "name": step.name,
            "status": step.status,
            "duration": step.duration,
            "message": step.message,
        })

    def finish(self, status: str) -> None:
        self.status = status
        self.duration = round(time.monotonic() - self.started_at, 2)
        # Sauvegarder le journal sur disque
        log_dir = LABS_ROOT / self.lab / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = log_dir / f"{ts}-{self.mode}.log"
        target.write_text("\n".join(self._log_lines) + "\n", encoding="utf-8")
        self.log_file = str(target.relative_to(REPO_ROOT))

        self.emit("done", {
            "status": self.status,
            "duration": self.duration,
            "log_file": self.log_file,
            "steps": [
                {"name": s.name, "status": s.status, "duration": s.duration, "message": s.message}
                for s in self.steps
            ],
        })
        for q in list(self.subscribers):
            q.put_nowait(None)

    async def stream(self) -> AsyncIterator[str]:
        """Génère le flux SSE (rejoue l'historique puis diffuse en direct)."""
        q: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
        for ev_type, payload in list(self.events):
            yield f"event: {ev_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        if self.status != "running":
            return

        self.subscribers.append(q)
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                ev_type, payload = item
                yield f"event: {ev_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            if q in self.subscribers:
                self.subscribers.remove(q)


class JobManager:
    """Gère les exécutions en cours et le verrou exclusif VM."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def _acquire_lock(self):
        lock_fd = open(LOCK_PATH, "w", encoding="utf-8")
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_fd.close()
            raise OperationInProgressError(
                "Une opération est déjà en cours sur la VM. Réessayez lorsqu'elle sera terminée."
            ) from exc
        lock_fd.write(f"{os.getpid()}\n")
        lock_fd.flush()
        return lock_fd

    def start_job(
        self,
        lab_name: str,
        mode: str,
        payload: LabPayload | None = None,
    ) -> Job:
        lock_fd = self._acquire_lock()
        step_names = (
            [
                "Validation & aperçu Containerlab",
                "Dry-run Ansible (--check --diff)",
            ]
            if mode == "preview"
            else [
                "Validation & NetBox",
                "Génération Containerlab",
                "Déploiement Containerlab",
                "Attente JSON-RPC SR Linux",
                "Configuration Ansible (deploy)",
                "Vérification BGP & Ping (verify)",
            ]
        )

        secrets = list(load_vault_secrets().values())
        env_tok = os.environ.get("NETBOX_TOKEN", "").strip()
        if env_tok:
            secrets.append(env_tok)

        job = Job(
            id=uuid.uuid4().hex[:12],
            lab=lab_name,
            mode=mode,
            steps=[StepStatus(name=n) for n in step_names],
            _secrets=secrets,
        )
        self.jobs[job.id] = job
        asyncio.create_task(self._run_with_lock(job, payload, lock_fd))
        return job

    async def _run_with_lock(self, job: Job, payload: LabPayload | None, lock_fd) -> None:
        try:
            if job.mode == "preview":
                await self._run_preview(job, payload)
            else:
                await self._run_apply(job, payload)
        except Exception as exc:
            job.log(f"[ERREUR] {exc}")
            for idx, step in enumerate(job.steps):
                if step.status == "running":
                    job.update_step(idx, "error", message=str(exc))
            job.finish("error")
        finally:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            finally:
                lock_fd.close()

    def _clab_path_for(self, lab_name: str) -> Path:
        if lab_name == PROTECTED_LAB:
            return REPO_ROOT / "netauto_lab.yml"
        return LABS_ROOT / lab_name / f"{lab_name}.clab.yml"

    def _ensure_ansible_inventory(self, lab_name: str) -> Path:
        if lab_name == PROTECTED_LAB:
            return ANSIBLE_DIR / "inventory-netbox" / "netbox.yml"

        lab_dir = LABS_ROOT / lab_name
        lab_dir.mkdir(parents=True, exist_ok=True)
        inv_path = lab_dir / "inventory.netbox.yml"
        inv_content = {
            "plugin": "netbox.netbox.nb_inventory",
            "api_endpoint": os.environ.get("NETBOX_URL", "https://127.0.0.1"),
            "validate_certs": False,
            "interfaces": True,
            "config_context": True,
            "flatten_config_context": True,
            "query_filters": [{"site": lab_name}],
            "group_by": ["device_roles"],
            "group_names_raw": True,
        }
        inv_path.write_text(yaml.safe_dump(inv_content, sort_keys=False), encoding="utf-8")

        gv_link = lab_dir / "group_vars"
        gv_target = ANSIBLE_DIR / "inventory" / "group_vars"
        if not gv_link.exists():
            gv_link.symlink_to(gv_target)
        return inv_path

    def _ansible_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["NETBOX_TOKEN"] = load_netbox_token()
        env.setdefault("ANSIBLE_VAULT_PASSWORD_FILE", str(Path("~/.vault_pass").expanduser()))
        env["PYTHONUNBUFFERED"] = "1"
        env["ANSIBLE_FORCE_COLOR"] = "0"
        return env

    async def _exec_stream(
        self,
        job: Job,
        cmd: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout: int = 180,
    ) -> int:
        job.log(f"$ {' '.join(cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        try:
            async def _read():
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    job.log(line.decode("utf-8", errors="replace"))

            await asyncio.wait_for(_read(), timeout=timeout)
            return await asyncio.wait_for(proc.wait(), timeout=15)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"Timeout ({timeout}s) dépassé pour : {cmd[0]}") from exc

    def _topology_changed(self, clab_path: Path, new_topo: dict[str, Any]) -> bool:
        """Compare nœuds et liens avec le fichier Containerlab existant."""
        if not clab_path.is_file():
            return True
        old = yaml.safe_load(clab_path.read_text(encoding="utf-8")) or {}
        new = generate_clab_dict(new_topo)
        if old.get("mgmt") != new.get("mgmt"):
            return True
        old_nodes = old.get("topology", {}).get("nodes", {})
        new_nodes = new.get("topology", {}).get("nodes", {})
        if set(old_nodes.keys()) != set(new_nodes.keys()):
            return True
        for name, ncfg in new_nodes.items():
            ocfg = old_nodes.get(name, {})
            if ocfg.get("kind") != ncfg.get("kind") or ocfg.get("mgmt-ipv4") != ncfg.get("mgmt-ipv4"):
                return True
        old_links = {frozenset(x["endpoints"]) for x in old.get("topology", {}).get("links", [])}
        new_links = {frozenset(x["endpoints"]) for x in new.get("topology", {}).get("links", [])}
        return old_links != new_links

    async def _wait_srlinux_jsonrpc(self, job: Job, topo: dict[str, Any], timeout: int = 120) -> None:
        targets = [
            (n["id"], n["mgmt_ip"].split("/")[0])
            for n in topo.get("nodes", [])
            if n.get("role") in ("router", "switch") and n.get("mgmt_ip")
        ]
        deadline = time.monotonic() + timeout
        pending = dict(targets)

        def _probe(ip: str) -> bool:
            try:
                r = requests.post(
                    f"https://{ip}/jsonrpc",
                    json={"jsonrpc": "2.0", "id": 1, "method": "get", "params": {"commands": []}},
                    verify=False,
                    timeout=3,
                )
                return r.status_code in (200, 401, 403)
            except requests.RequestException:
                return False

        while pending and time.monotonic() < deadline:
            for nid, ip in list(pending.items()):
                ok = await asyncio.to_thread(_probe, ip)
                if ok:
                    job.log(f"  ✓ {nid} ({ip}) répond en JSON-RPC")
                    pending.pop(nid, None)
            if pending:
                await asyncio.sleep(3)

        if pending:
            missing = ", ".join(f"{k} ({v})" for k, v in pending.items())
            raise RuntimeError(f"SR Linux ne répond pas en JSON-RPC après {timeout}s : {missing}")

    async def _run_preview(self, job: Job, payload: LabPayload | None) -> None:
        client = NetBoxClient()

        # Étape 0 : validation + aperçu du diff Containerlab (sans écrire)
        t0 = time.monotonic()
        job.update_step(0, "running")
        if payload is not None:
            # Validation stricte du brouillon
            from webui.topology import validate_lab_payload
            validate_lab_payload(payload, allow_netauto=(job.lab == PROTECTED_LAB))
            job.log("Topologie brouillon valide.")
        topo = await asyncio.to_thread(client.get_lab_topology, job.lab)
        clab_path = self._clab_path_for(job.lab)
        old_text = clab_path.read_text(encoding="utf-8") if clab_path.is_file() else ""
        new_text = generate_clab_yaml(topo)
        diff = compute_yaml_diff(old_text, new_text, filename=clab_path.name)
        if diff:
            job.log(f"--- Diff Containerlab ({clab_path.name}) ---")
            for line in diff.splitlines():
                job.log(line)
        else:
            job.log(f"Aucun changement dans {clab_path.name}.")
        job.update_step(0, "ok", time.monotonic() - t0)

        # Étape 1 : dry-run Ansible --check --diff
        t1 = time.monotonic()
        job.update_step(1, "running")
        inv = self._ensure_ansible_inventory(job.lab)
        ap_bin = str(VENV_BIN / "ansible-playbook")
        rc = await self._exec_stream(
            job,
            [ap_bin, "-i", str(inv), "deploy.yml", "--check", "--diff"],
            cwd=ANSIBLE_DIR,
            env=self._ansible_env(),
            timeout=180,
        )
        if rc != 0:
            raise RuntimeError(f"Dry-run Ansible en échec (code {rc}).")
        job.update_step(1, "ok", time.monotonic() - t1)
        job.finish("ok")

    async def _run_apply(self, job: Job, payload: LabPayload | None) -> None:
        client = NetBoxClient()

        # Étape 0 : validation & enregistrement NetBox
        t0 = time.monotonic()
        job.update_step(0, "running")
        if payload is not None:
            job.log(f"Validation et enregistrement du lab '{job.lab}' dans NetBox...")
            planned = await asyncio.to_thread(
                plan_lab_topology, payload, client, allow_netauto=(job.lab == PROTECTED_LAB)
            )
            topo = await asyncio.to_thread(client.save_lab_topology, planned)
            job.log(f"NetBox mis à jour : {len(topo['nodes'])} nœuds, {len(topo['links'])} liens.")
        else:
            topo = await asyncio.to_thread(client.get_lab_topology, job.lab)
            job.log(f"Topologie lue depuis NetBox : {len(topo['nodes'])} nœuds, {len(topo['links'])} liens.")
        job.update_step(0, "ok", time.monotonic() - t0)

        # Étape 1 : génération Containerlab & diff
        t1 = time.monotonic()
        job.update_step(1, "running")
        clab_path = self._clab_path_for(job.lab)
        if job.lab == PROTECTED_LAB and clab_path.is_file():
            orig_backup = REPO_ROOT / "netauto_lab.yml.orig"
            if not orig_backup.is_file():
                shutil.copy2(clab_path, orig_backup)
                job.log("Sauvegarde initiale créée : netauto_lab.yml.orig")

        topo_changed = self._topology_changed(clab_path, topo)
        old_text = clab_path.read_text(encoding="utf-8") if clab_path.is_file() else ""
        new_text = generate_clab_yaml(topo)
        diff = compute_yaml_diff(old_text, new_text, filename=clab_path.name)
        clab_path.parent.mkdir(parents=True, exist_ok=True)
        if new_text != old_text:
            clab_path.write_text(new_text, encoding="utf-8")
            job.log(f"Fichier mis à jour : {clab_path.relative_to(REPO_ROOT)}")
            for line in diff.splitlines():
                job.log(line)
        else:
            job.log(f"Fichier {clab_path.name} déjà à jour.")
        job.update_step(1, "ok", time.monotonic() - t1)

        # Étape 2 : Containerlab deploy (si topologie changée, lab arrêté, ou liens veth perdus après reboot VM)
        t2 = time.monotonic()
        job.update_step(2, "running")
        inspect_rc = await self._exec_stream(
            job,
            ["sudo", "/usr/bin/containerlab", "inspect", "-t", str(clab_path)],
            cwd=REPO_ROOT,
            timeout=30,
        )
        lab_running = inspect_rc == 0

        # Vérifier qu'au moins un client possède bien son interface eth1 (disparaît après un reboot VM)
        links_intact = True
        if lab_running:
            first_client = next((n for n in topo.get("nodes", []) if n.get("role") == "container"), None)
            if first_client:
                chk = await asyncio.create_subprocess_exec(
                    "docker", "exec", first_client["device_name"], "ip", "link", "show", "eth1",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                links_intact = (await chk.wait()) == 0
                if not links_intact:
                    job.log("Liens veth Containerlab absents (probable redémarrage VM) : redéploiement forcé.")

        if topo_changed or not lab_running or not links_intact:
            rc = await self._exec_stream(
                job,
                ["sudo", "/usr/bin/containerlab", "deploy", "-t", str(clab_path), "--reconfigure"],
                cwd=REPO_ROOT,
                timeout=300,
            )
            if rc != 0:
                raise RuntimeError(f"containerlab deploy a échoué (code {rc}).")
            job.update_step(2, "ok", time.monotonic() - t2)
        else:
            job.log("Topologie Containerlab inchangée et lab actif : conteneurs conservés.")
            job.update_step(2, "skipped", time.monotonic() - t2, "Aucun changement")

        # Étape 3 : attente JSON-RPC SR Linux
        t3 = time.monotonic()
        job.update_step(3, "running")
        await self._wait_srlinux_jsonrpc(job, topo, timeout=120)
        job.update_step(3, "ok", time.monotonic() - t3)

        # Étape 4 : Ansible deploy.yml --diff
        t4 = time.monotonic()
        job.update_step(4, "running")
        inv = self._ensure_ansible_inventory(job.lab)
        ap_bin = str(VENV_BIN / "ansible-playbook")
        rc = await self._exec_stream(
            job,
            [ap_bin, "-i", str(inv), "deploy.yml", "--diff"],
            cwd=ANSIBLE_DIR,
            env=self._ansible_env(),
            timeout=180,
        )
        if rc != 0:
            raise RuntimeError(f"ansible-playbook deploy.yml a échoué (code {rc}).")
        job.update_step(4, "ok", time.monotonic() - t4)

        # Étape 5 : Ansible verify.yml (BGP + ping)
        t5 = time.monotonic()
        job.update_step(5, "running")
        rc = await self._exec_stream(
            job,
            [ap_bin, "-i", str(inv), "verify.yml"],
            cwd=ANSIBLE_DIR,
            env=self._ansible_env(),
            timeout=300,
        )
        if rc != 0:
            raise RuntimeError(f"ansible-playbook verify.yml a échoué (code {rc}).")
        job.update_step(5, "ok", time.monotonic() - t5)

        job.finish("ok")

    def destroy_lab(self, lab_name: str) -> dict[str, Any]:
        """Détruit un lab (Containerlab --cleanup + objets NetBox + dossier labs/<lab>)."""
        if lab_name == PROTECTED_LAB:
            raise PermissionError("Le lab 'netauto' est protégé contre la suppression.")

        lock_fd = self._acquire_lock()
        try:
            clab_path = self._clab_path_for(lab_name)
            clab_destroyed = False
            if clab_path.is_file():
                import subprocess
                proc = subprocess.run(
                    ["sudo", "/usr/bin/containerlab", "destroy", "-t", str(clab_path), "--cleanup"],
                    cwd=str(REPO_ROOT),
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                clab_destroyed = proc.returncode == 0

            client = NetBoxClient()
            client.delete_lab(lab_name)

            lab_dir = LABS_ROOT / lab_name
            if lab_dir.is_dir():
                shutil.rmtree(lab_dir, ignore_errors=True)

            return {"deleted": lab_name, "containerlab_destroyed": clab_destroyed}
        finally:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            finally:
                lock_fd.close()


job_manager = JobManager()

"""Application web FastAPI — NetAuto Studio."""
from __future__ import annotations

from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pydantic import BaseModel

from webui.generator import generate_clab_yaml
from webui.inspector import get_lab_live_status, run_client_ping
from webui.netbox_client import LabNotFoundError, NetBoxClient, NetBoxError, PROTECTED_LAB
from webui.runner import OperationInProgressError, job_manager
from webui.topology import (
    LabPayload,
    TopologyValidationError,
    plan_lab_topology,
    validate_lab_payload,
)

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="NetAuto Studio", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def get_netbox_client() -> NetBoxClient:
    """Instancie un client NetBox pour la requête courante."""
    try:
        return NetBoxClient()
    except NetBoxError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Page principale de NetAuto Studio."""
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    """Point de contrôle de santé du serveur."""
    return {"status": "ok"}


@app.get("/api/labs")
def api_list_labs() -> list[dict]:
    """Liste tous les labs présents dans NetBox."""
    client = get_netbox_client()
    try:
        return client.list_labs()
    except NetBoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/labs/{lab_name}")
def api_get_lab(lab_name: str) -> dict:
    """Retourne la topologie complète (nœuds et liens) d'un lab."""
    client = get_netbox_client()
    try:
        return client.get_lab_topology(lab_name)
    except LabNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NetBoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/labs/{lab_name}/clab")
def api_get_lab_clab(lab_name: str) -> dict[str, str]:
    """Génère et retourne le fichier Containerlab YAML correspondant au lab."""
    client = get_netbox_client()
    try:
        topo = client.get_lab_topology(lab_name)
        yaml_text = generate_clab_yaml(topo)
        return {"lab": lab_name, "filename": f"{lab_name}.clab.yml", "yaml": yaml_text}
    except LabNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (NetBoxError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/labs/validate")
def api_validate_lab(payload: LabPayload) -> dict[str, bool]:
    """Valide une topologie sans écrire dans NetBox."""
    try:
        validate_lab_payload(payload)
        return {"valid": True}
    except TopologyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/labs", status_code=201)
def api_create_lab(payload: LabPayload) -> dict:
    """Crée un nouveau lab dans NetBox avec allocation IPAM/ASN/BGP automatique."""
    try:
        validate_lab_payload(payload)
    except TopologyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    client = get_netbox_client()
    try:
        existing_names = {lab["name"] for lab in client.list_labs()}
        if payload.name.strip() in existing_names:
            raise HTTPException(
                status_code=409,
                detail=f"Un lab nommé '{payload.name.strip()}' existe déjà dans NetBox.",
            )
        planned = plan_lab_topology(payload, client)
        saved = client.save_lab_topology(planned)
        return saved
    except TopologyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except NetBoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.put("/api/labs/{lab_name}")
def api_update_lab(lab_name: str, payload: LabPayload) -> dict:
    """Met à jour un lab existant dans NetBox (y compris netauto)."""
    if payload.name.strip() != lab_name:
        raise HTTPException(
            status_code=422,
            detail="Le nom du lab dans le corps ne correspond pas à l'URL.",
        )

    client = get_netbox_client()
    try:
        planned = plan_lab_topology(
            payload, client, allow_netauto=(lab_name == PROTECTED_LAB)
        )
        saved = client.save_lab_topology(planned)
        return saved
    except TopologyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except NetBoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.delete("/api/labs/{lab_name}")
def api_delete_lab(lab_name: str) -> dict:
    """Détruit un lab entier (Containerlab + NetBox + fichiers locaux). Interdit sur netauto."""
    if lab_name == PROTECTED_LAB:
        raise HTTPException(
            status_code=403,
            detail="Le lab 'netauto' est protégé contre la suppression.",
        )
    try:
        return job_manager.destroy_lab(lab_name)
    except OperationInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LabNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NetBoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/labs/{lab_name}/preview")
async def api_preview_lab(
    lab_name: str, payload: LabPayload | None = Body(default=None)
) -> dict:
    """Lance un aperçu (diff Containerlab + Ansible --check --diff) sans rien appliquer."""
    try:
        job = job_manager.start_job(lab_name, "preview", payload)
    except OperationInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "job_id": job.id,
        "mode": job.mode,
        "steps": [{"name": s.name, "status": s.status} for s in job.steps],
    }


@app.post("/api/labs/{lab_name}/apply")
async def api_apply_lab(
    lab_name: str, payload: LabPayload | None = Body(default=None)
) -> dict:
    """Lance le pipeline complet : NetBox -> Containerlab -> JSON-RPC -> Ansible -> Verify."""
    try:
        job = job_manager.start_job(lab_name, "apply", payload)
    except OperationInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "job_id": job.id,
        "mode": job.mode,
        "steps": [{"name": s.name, "status": s.status} for s in job.steps],
    }


@app.get("/api/jobs/{job_id}/stream")
async def api_stream_job(job_id: str) -> StreamingResponse:
    """Diffuse les étapes et les logs d'une exécution en temps réel (SSE)."""
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Exécution introuvable.")
    return StreamingResponse(
        job.stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class PingRequest(BaseModel):
    source: str
    target: str
    count: int = 3


@app.get("/api/labs/{lab_name}/status")
def api_lab_status(lab_name: str) -> dict:
    """Retourne l'état temps réel (interfaces + sessions BGP) des équipements SR Linux."""
    client = get_netbox_client()
    try:
        return get_lab_live_status(lab_name, client=client)
    except LabNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NetBoxError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/labs/{lab_name}/ping")
def api_lab_ping(lab_name: str, req: PingRequest) -> dict:
    """Exécute un ping entre deux clients choisis dans l'interface."""
    client = get_netbox_client()
    try:
        return run_client_ping(lab_name, req.source, req.target, count=req.count, client=client)
    except LabNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erreur lors du ping : {exc}") from exc

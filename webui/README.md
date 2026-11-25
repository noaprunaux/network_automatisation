# NetAuto Studio (`webui/`)

Interface web d'orchestration des labs réseau s'appuyant sur la chaîne existante :
**NetBox (source de vérité) → Containerlab → Ansible (JSON-RPC SR Linux) → Vérification BGP/Ping → Supervision Grafana**.

## Architecture

```
Navigateur ──HTTPS (nginx /studio/)──► FastAPI / Uvicorn (127.0.0.1:8080)
                                          ├─► webui/netbox_client.py : lecture/écriture & IPAM NetBox 4.x
                                          ├─► webui/topology.py      : validation métier, IPAM, ASN, dérivation BGP
                                          ├─► webui/generator.py     : générateur déterministe NetBox -> .clab.yml
                                          ├─► webui/runner.py        : Containerlab + Ansible en sous-processus + SSE + verrou fcntl
                                          └─► webui/inspector.py     : état BGP/interfaces JSON-RPC + ping docker exec
```

- **Front** : page unique HTML/CSS/JS vanilla (`webui/templates/index.html`, `webui/static/`), graphe statique par couches (`vis-network`), console temps réel SSE, onglets **Topologie**, **Tests** et **Supervision** (Grafana en iframe `/grafana/`).
- **Sécurité** :
  - Écoute sur `127.0.0.1:8080` uniquement (accès via tunnel SSH ou reverse proxy nginx HTTPS `/studio/`).
  - Aucun secret dans le code ni côté navigateur : `NETBOX_TOKEN` et identifiants SR Linux lus côté serveur depuis l'environnement ou le vault Ansible (`~/.vault_pass`), et masqués (`***`) dans tous les logs.
  - Aucun `shell=True` ; validation stricte par regex/Pydantic ; lab `netauto` protégé contre la suppression (`403`).
  - Verrou exclusif `/tmp/netauto.lock` (`fcntl`) pour empêcher deux déploiements simultanés.

## Installation

Depuis la racine du dépôt sur la VM `tp-automatisation` :

```bash
source ~/venv/bin/activate
pip install -r webui/requirements.txt
```

## Lancement

### Manuel (développement)
```bash
source ~/venv/bin/activate
uvicorn webui.main:app --host 127.0.0.1 --port 8080
```

Accès via tunnel SSH :
```bash
ssh -L 8080:127.0.0.1:8080 debian@<IP-VM>
# puis ouvrir http://127.0.0.1:8080
```

### Service `systemd` (production sur la VM)
Le service `/etc/systemd/system/netauto-webui.service` maintient le serveur actif en permanence :
```bash
sudo systemctl status netauto-webui
sudo systemctl restart netauto-webui
journalctl -u netauto-webui -f
```

## Principaux endpoints API

| Méthode | Endpoint | Rôle |
|---|---|---|
| `GET` | `/api/labs` | Liste les labs (sites NetBox) |
| `GET` | `/api/labs/{lab}` | Topologie complète (nœuds, interfaces, IP, câbles, BGP) |
| `GET` | `/api/labs/{lab}/clab` | Fichier Containerlab YAML généré |
| `POST` | `/api/labs` | Crée un nouveau lab dans NetBox (IPAM/ASN/BGP auto) |
| `PUT` | `/api/labs/{lab}` | Met à jour un lab existant dans NetBox |
| `DELETE` | `/api/labs/{lab}` | Détruit un lab (Containerlab + NetBox ; `403` sur `netauto`) |
| `POST` | `/api/labs/{lab}/preview` | Aperçu dry-run (diff Containerlab + Ansible `--check --diff`) |
| `POST` | `/api/labs/{lab}/apply` | Déploiement complet (NetBox → Containerlab → Ansible → Verify) |
| `GET` | `/api/jobs/{job_id}/stream` | Flux Server-Sent Events (SSE) des étapes et logs |
| `GET` | `/api/labs/{lab}/status` | État temps réel BGP & interfaces (SR Linux JSON-RPC) |
| `POST` | `/api/labs/{lab}/ping` | Ping entre deux clients choisis (`docker exec`) |

## Tests unitaires (`pytest`)

Les tests utilisent des mocks NetBox/IPAM/JSON-RPC et ne dépendent pas du lab réel :

```bash
source ~/venv/bin/activate
pytest -v webui/tests
```

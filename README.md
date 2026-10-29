# garmin-mcp

Serveur MCP (lecture seule) basé sur l'**API officielle Garmin Connect Personal**
(`https://personal.connectapi.garmin.com`, spec dans `spec/openapi.json`). Il tourne en parallèle de `garmin-bridge` (qui, lui, s'appuie sur GarminDB et sur l'API non officielle).

## Fonctionnement

- La clé personnelle Garmin (`GARMIN_PERSONAL_API_KEY`) est échangée contre un access token via
  `POST /personalapitoken-service/personal-api/personal-api-key/token` (`Authorization: ApiKey …`).
  Le token est gardé en mémoire, renouvelé 30 s avant son expiration, et redemandé une fois si Garmin répond 401.
- Transport MCP streamable HTTP (stateless) sur `/mcp`, port interne 8001. `GET /health` (sans authentification) sert au healthcheck Docker.

### Tools

27 tools (26 tools Garmin + `server_health`). Les dates sont au format `YYYY-MM-DD` et les plages sont inclusives.

| Domaine | Tools | Plage max |
|---|---|---|
| Activités | `list_activities`, `get_activity`, `get_activity_workouts`, `get_activity_file`, `get_wellness_activity_summaries` | 366 j |
| Résumés quotidiens | `get_daily_summary`, `get_daily_summaries` | 366 j |
| Pas, stress | `get_steps`, `get_stress` | 28 j |
| Hydratation, sommeil, Body Battery | `get_hydration`, `get_sleep`, `get_sleep_summaries`, `get_body_battery` | 31 j |
| HRV, FC, SpO2 | `get_hrv`, `get_hrv_summaries`, `get_heart_rate`, `get_spo2` | 366 j |
| Poids | `get_weight`, `get_weight_entries`, `get_weight_goals` | 366 j |
| Profil | `get_fitness_age`, `get_heart_rate_zones`, `get_user_settings` | — |
| Nutrition, cycle | `get_nutrition_log`, `get_menstrual_cycle_summary`, `get_menstrual_cycle_day` | — |

`get_activity_file` renvoie le fichier d'origine (zip contenant le FIT) en base64. Au-delà de `MAX_FILE_BYTES`, le téléchargement est interrompu et refusé.

### Erreurs

Les erreurs Garmin sont renvoyées au client MCP comme erreurs de tool (`isError`), avec un message court et sans secret : clé personnelle refusée, authentification échouée, 403, 404, rate limit Garmin (429), erreur serveur, redirection inattendue, JSON invalide ou API injoignable. Une réponse vide (204) donne `{"data": null, "note": "No data for this request"}`.

## Sécurité

- `MCP_API_KEY` (≥ 32 caractères) obligatoire, **uniquement** via le header `Authorization: Bearer <clé>`. Une clé passée dans l'URL (`?api_key=`) est refusée : elle finirait dans les logs du proxy et l'historique. La comparaison se fait sur les octets bruts, en temps constant.
- Validation du header `Host` / `Origin` (anti DNS rebinding) sur `DOMAIN` : un autre hôte reçoit un 421, une autre origine un 403.
- Les connexions websocket sont refusées.
- Logs d'accès uvicorn désactivés. Les erreurs renvoyées au client ne contiennent jamais de secret.
- Aucun port publié sur l'hôte : uniquement `expose`, accès via nginx-proxy sur le réseau externe `reseau_interne`.
- Conteneur non-root, FS en lecture seule, `cap_drop: ALL`, `no-new-privileges`, limites mémoire (256 Mo) et PIDs.

## Configuration

| Variable | Défaut | Rôle |
|---|---|---|
| `MCP_API_KEY` | requis | Clé du endpoint MCP (≥ 32 caractères, `openssl rand -hex 32`) |
| `GARMIN_PERSONAL_API_KEY` | requis | Clé personnelle Garmin Connect (elle expire) |
| `DOMAIN` | requis | Hôte public accepté (docker-compose le remplit avec `VIRTUAL_HOST`) |
| `PORT` | `8001` | Port d'écoute |
| `HTTP_TIMEOUT` | `30` | Timeout des appels Garmin (s) |
| `MAX_FILE_BYTES` | `5242880` | Taille max d'un fichier d'activité (octets, avant base64) |
| `GARMIN_BASE_URL` | `https://personal.connectapi.garmin.com` | URL de l'API Garmin |

## Déploiement

```bash
cp .env.example .env   # renseigner VIRTUAL_HOST, MCP_API_KEY et GARMIN_PERSONAL_API_KEY
docker compose up -d --build
```

Client MCP : URL `https://<VIRTUAL_HOST>/mcp`, avec le header `Authorization: Bearer <MCP_API_KEY>`.

> Les anciennes configurations qui passaient la clé dans l'URL (`/mcp?api_key=…`), comme un connecteur claude.ai, reçoivent maintenant un 401. Configure le header `Authorization` dans le client.

`GARMIN_PERSONAL_API_KEY` expire (durée choisie à la création) : en cas d'erreur « Garmin rejected the personal API key », en générer une nouvelle sur Garmin Connect web, mettre à jour `.env` et relancer le conteneur.

## CI

`.github/workflows/docker.yml`, à chaque push sur `main` ou sur un tag `v*` :

1. lance les tests (`pytest`, Python 3.12) ; l'image n'est pas publiée s'ils échouent ;
2. build et pousse `<DOCKERHUB_USERNAME>/garmin-mcp:latest` et `:<branche ou tag>` (`:main` ou `:v1.2.3`) ;
3. appelle le webhook n8n de redéploiement.

Secrets requis : `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`, `N8N_WEBHOOK_ID`.

## Dev local

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cd src && MCP_API_KEY=… GARMIN_PERSONAL_API_KEY=… DOMAIN=localhost ../.venv/bin/python -m garmin_mcp
```

Vérification rapide (liste des tools) :

```bash
curl -s http://localhost:8001/mcp \
  -H "Authorization: Bearer $MCP_API_KEY" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Tests

```bash
.venv/bin/pytest
```

Les tests couvrent l'authentification et la limite de tentatives (`tests/test_security.py`), le client Garmin (`tests/test_garmin_client.py` : token, erreurs, fichiers) et les tools et l'app HTTP (`tests/test_server.py`). Ils n'appellent jamais Garmin (transport httpx simulé), n'ont besoin d'aucune vraie clé et ignorent les variables d'environnement du shell.

# garmin-mcp

Serveur MCP (lecture seule) basé sur l'**API officielle Garmin Connect Personal**
(`https://personal.connectapi.garmin.com`, spec dans `spec/openapi.json`). Il tourne en parallèle de `garmin-bridge` (qui, lui, s'appuie sur GarminDB et sur l'API non officielle).

## Fonctionnement

- La clé personnelle Garmin (`GARMIN_PERSONAL_API_KEY`) est échangée contre un access token via
  `POST /personalapitoken-service/personal-api/personal-api-key/token` (`Authorization: ApiKey …`).
  Le token est gardé en mémoire et renouvelé à l'expiration ou sur un 401.
- 27 tools couvrant activités, résumés quotidiens, sommeil, HRV, FC, Body Battery, SpO2, poids, stress, pas, hydratation, âge de forme, zones de FC, profil, nutrition et cycle.
- Transport MCP streamable HTTP (stateless) sur `/mcp`, port interne 8001.

## Sécurité

- `MCP_API_KEY` (≥ 32 caractères) obligatoire : `Authorization: Bearer <clé>` ou `?api_key=` / `?api-key=` (la query est volontaire, pour les connecteurs claude.ai). Comparaison en temps constant.
- Blocage temporaire (429) après 10 échecs en 5 min par IP (`X-Forwarded-For` posé par nginx-proxy).
- Validation du header `Host` / `Origin` (anti DNS rebinding) sur `DOMAIN`.
- Logs d'accès uvicorn désactivés (la clé peut être dans l'URL).
- Aucun port publié sur l'hôte : uniquement `expose`, accès via nginx-proxy sur le réseau externe `reseau_interne`.
- Conteneur non-root, FS en lecture seule, `cap_drop: ALL`, `no-new-privileges`, limites mémoire/PIDs.
- Les erreurs renvoyées au client ne contiennent jamais de secret.

## Déploiement

```bash
cp .env.example .env   # renseigner MCP_API_KEY (openssl rand -hex 32) et GARMIN_PERSONAL_API_KEY
docker compose up -d --build
```

URL à renseigner dans le connecteur : `https://<VIRTUAL_HOST>/mcp?api_key=<MCP_API_KEY>`
(ou header `Authorization: Bearer`).

`GARMIN_PERSONAL_API_KEY` expire (durée choisie à la création) : en cas d'erreur « Garmin rejected the personal API key », en générer une nouvelle sur Garmin Connect web.

## CI

`.github/workflows/docker.yml` build et pousse `<DOCKERHUB_USERNAME>/garmin-mcp` (`latest` + tag) puis appelle le webhook n8n.
Secrets requis : `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`, `N8N_WEBHOOK_ID`.

## Dev local

```bash
pip install -r requirements.txt
cd src && MCP_API_KEY=… GARMIN_PERSONAL_API_KEY=… DOMAIN=localhost python -m garmin_mcp
```

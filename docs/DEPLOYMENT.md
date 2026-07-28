# Deploy produzione

## Scelta consigliata

La generazione video e FFmpeg non sono adatti a funzioni serverless brevi. Usare uno di questi assetti:

1. **VPS/Docker con volume persistente**: percorso più semplice per l'MVP.
2. **Cloud Run**: servizio web + worker separato + Neon/PostgreSQL + bucket Cloud Storage.
3. **Kubernetes**: solo quando il volume di produzioni lo giustifica.

Vercel può ospitare una landing page pubblica, ma non è il posto corretto per il worker multimediale principale.

## MVP su VPS

```bash
git clone REPOSITORY_URL
cd nuvibu
cp .env.example .env
# compilare .env
docker compose up -d --build
```

Montare `storage/` e `secrets/` su volumi persistenti, mettere la console dietro HTTPS e non esporre il worker.

## Cloud Run

Componenti:

- `nuvibu-web`: FastAPI, accesso amministrativo;
- `nuvibu-worker`: processo `python scripts/run_worker.py`;
- Neon/PostgreSQL: stato dei job e metadati;
- Cloud Storage: input, scene e render;
- Secret Manager: chiavi ElevenLabs, service account e token OAuth;
- Cloud Logging: errori e durata job.

Il codice incluso usa filesystem locale per gli asset. Per scalare orizzontalmente va aggiunto un adapter object-storage oppure montato uno storage condiviso. Non avviare più worker sul database SQLite.

## Variabili minime live

```dotenv
APP_ENV=production
ADMIN_USERNAME=...
ADMIN_PASSWORD=...
SECRET_KEY=...
DATABASE_URL=postgresql://...
PROVIDER_MODE=live
ELEVENLABS_API_KEY=...
GOOGLE_CLOUD_PROJECT=...
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_APPLICATION_CREDENTIALS=/secrets/google-service-account.json
VEO_OUTPUT_GCS_URI=gs://bucket/prefix/
YOUTUBE_CLIENT_SECRETS_FILE=/secrets/youtube-client-secret.json
YOUTUBE_TOKEN_FILE=/secrets/youtube-token.json
```

## Gate prima del go-live

- test mock completi;
- una generazione live musicale verificata parola per parola;
- una scena live per ogni tipo di movimento;
- controllo costo effettivo contro il preventivo;
- OAuth YouTube con upload privato;
- restore testato del database;
- chiavi fuori dalla repository;
- tre episodi pronti prima della prima pubblicazione.

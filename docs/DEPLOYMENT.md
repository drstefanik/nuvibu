# Deploy produzione

La produzione usa:

- Cloud Run Service `nuvibu-web` per la console;
- Cloud Run Job `nuvibu-worker` per provider e FFmpeg;
- Neon/PostgreSQL per episodi, job e ledger;
- un bucket Cloud Storage montato su `/mnt/nuvibu`;
- Secret Manager per tutte le credenziali;
- Vertex AI con `veo-3.1-generate-001` e autenticazione ADC;
- ElevenLabs Music.

Vercel non è necessario per questa architettura.

## Prerequisiti

1. Il progetto Google Cloud deve essere `nuvibu` e avere la fatturazione attiva.
2. La migrazione `migrations/0001_initial.sql` deve essere applicata a Neon.
3. Secret Manager deve contenere `elevenlabs-api-key`.
4. Recuperare da Neon la connection string **pooled** del branch `production`.
5. Verificare che il progetto abbia accesso e quota per Veo in `us-central1`.

Il deploy predefinito usa Vertex AI tramite l'identità
`nuvibu-worker@nuvibu.iam.gserviceaccount.com`. Cloud Run fornisce
automaticamente Application Default Credentials: non creare o scaricare chiavi
JSON e non impostare `GOOGLE_APPLICATION_CREDENTIALS` in produzione.

## Deploy guidato da Cloud Shell

Aprire Cloud Shell nel progetto `nuvibu`, clonare la repository e lanciare:

```bash
git clone https://github.com/drstefanik/nuvibu.git
cd nuvibu
./deploy/cloud-run.sh
```

I valori predefiniti sono:

```dotenv
GOOGLE_CLOUD_PROJECT=nuvibu
CLOUD_RUN_REGION=us-central1
GOOGLE_CLOUD_LOCATION=us-central1
VEO_BACKEND=vertex
VEO_MODEL=veo-3.1-generate-001
```

Lo script:

- abilita le API necessarie;
- crea Artifact Registry, bucket e service account separati;
- chiede in input nascosto la URL Neon e le credenziali della console;
- crea una chiave applicativa casuale;
- concede a ogni runtime soltanto i segreti che usa;
- concede `roles/aiplatform.user` soltanto al worker;
- crea il service agent Vertex AI e gli concede accesso al solo bucket media;
- blocca nel deploy le versioni numeriche correnti dei segreti;
- compila un'immagine dal commit corrente;
- configura il mount GCS con UID/GID `10001`;
- configura `VEO_OUTPUT_GCS_URI` nello stesso bucket montato;
- crea un job con una sola task, un'ora di timeout e zero retry automatici;
- autorizza il web a eseguire quel job con l'ID esatto del job applicativo;
- pubblica la console con Basic Auth su HTTPS.

I retry Cloud Run sono intenzionalmente disattivati: la pipeline salva ogni
asset completato e l'ID dell'operazione Veo, quindi una ripresa esplicita non
duplica una generazione pagata.

## Variabili rilevanti

```dotenv
APP_ENV=production
RUNTIME_ROLE=web|worker
PROVIDER_MODE=live
DATABASE_URL=postgresql://...
STORAGE_BACKEND=gcs_mount
STORAGE_ROOT=/mnt/nuvibu
VEO_BACKEND=vertex
VEO_MODEL=veo-3.1-generate-001
GOOGLE_CLOUD_PROJECT=nuvibu
GOOGLE_CLOUD_LOCATION=us-central1
VEO_OUTPUT_GCS_URI=gs://nuvibu-media-PROJECT_NUMBER/veo-output/
MAX_EPISODE_SECONDS=30
MAX_MUSIC_VARIANTS=1
MAX_SCENE_RETRIES=0
MAX_ESTIMATED_COST_USD_PER_EPISODE=10
```

Il servizio web non riceve chiavi provider. Il worker riceve soltanto la chiave
ElevenLabs; Vertex usa ADC. Il worker non riceve le credenziali amministrative
della console.

## Backend Gemini opzionale

Gemini Developer API non è necessaria per il deploy predefinito. Per usarla
esplicitamente:

1. Importare/selezionare `nuvibu` in Google AI Studio e attivare il piano Gemini
   API a pagamento.
2. Creare una nuova authorization key e salvarla in Secret Manager come
   `gemini-api-key`.
3. Eseguire:

```bash
VEO_BACKEND=gemini \
VEO_MODEL=veo-3.1-fast-generate-preview \
./deploy/cloud-run.sh
```

Solo in questa modalità lo script abilita `generativelanguage.googleapis.com`,
richiede `gemini-api-key` e la espone al worker. Le nuove authorization key
create da AI Studio sono già limitate a Gemini.

## Verifica dopo il deploy

```bash
curl -fsS "$(gcloud run services describe nuvibu-web \
  --project nuvibu --region us-central1 --format='value(status.url)')/healthz"

curl -fsS "$(gcloud run services describe nuvibu-web \
  --project nuvibu --region us-central1 --format='value(status.url)')/readyz"
```

La seconda chiamata deve restituire database e storage `ok`. La console richiede
le credenziali Basic Auth definite durante il deploy.

## Primo pilota

1. Creare un episodio di 20–30 secondi.
2. Caricare la reference approvata di Nuvibù.
3. Controllare la stima costo prima di accodare.
4. Accodare una sola volta e seguire lo stato del worker nella pagina episodio.
5. Revisionare integralmente testo, audio, scene, Short e QC.
6. Non pubblicare la thumbnail di concept: resta marcata `CONCEPT PREVIEW`.

YouTube è una fase successiva. Il token OAuth deve stare in un percorso
scrivibile del bucket montato, perché il client aggiorna il refresh token; non
va montato come file Secret Manager in sola lettura.

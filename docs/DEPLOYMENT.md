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
- pubblica la console con login amministratore e sessione HTTPS sicura;
- mantiene una istanza web pronta, abilita lo startup CPU boost e può
  scalare fino a tre istanze, riducendo i 429 `Rate exceeded.` di Cloud Run
  durante cold start o indisponibilità temporanee della singola istanza.

Se una revisione precedente è già online con `min-instances=0` e
`max-instances=1`, la correzione può essere applicata immediatamente senza
ricompilare l'immagine. I limiti seguenti sono a livello di servizio, quindi
restano complessivi anche durante il passaggio tra revisioni:

```bash
gcloud run services update nuvibu-web \
  --project nuvibu \
  --region us-central1 \
  --min 1 \
  --max 3 \
  --min-instances default \
  --max-instances default \
  --concurrency 20 \
  --cpu-boost
```

I due valori `default` rimuovono il precedente limite ereditato dalla
revisione (`max-instances=1`), che altrimenti continuerebbe a prevalere sul
limite di servizio.

L'istanza minima comporta un costo infrastrutturale ricorrente di
Cloud Run. Il limite giornaliero Nuvibù da 60 USD protegge invece la spesa dei
provider di generazione e non include Cloud Run: mantenere anche un alert di
fatturazione Google Cloud.

Il blocco dei tentativi di login è una difesa aggiuntiva locale a ciascuna
istanza. Con più repliche la protezione primaria resta la password
amministrativa ad alta entropia in Secret Manager; un limite globale per IP
richiede un load balancer con Cloud Armor o uno store condiviso.

I retry Cloud Run sono intenzionalmente disattivati: la pipeline salva ogni
asset completato e l'ID dell'operazione Veo, quindi una ripresa esplicita non
duplica una generazione pagata.

Il tetto giornaliero include sia i costi già registrati sia le prenotazioni dei
job attivi. Se l'esito di una chiamata ElevenLabs o Veo è ambiguo, lo stesso job
resta in attesa con la prenotazione conservata finché la ricevuta o l'operazione
non viene ripresa o riconciliata. Gli asset pagati ma non più validi restano nel
ledger storico: non eliminare job, ricevute o righe di costo per aggirare questo
blocco.

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
MAX_DAILY_ESTIMATED_COST_USD=0
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
  --project nuvibu --region us-central1 --format='value(status.url)')/health"

curl -fsS "$(gcloud run services describe nuvibu-web \
  --project nuvibu --region us-central1 --format='value(status.url)')/readyz"
```

La seconda chiamata deve restituire database e storage `ok`. La console mostra
la pagina di accesso e usa le credenziali amministratore definite durante il
deploy.

## Primo pilota

1. Accedere alla console con le credenziali amministratore del deploy.
2. Creare un episodio di 20–30 secondi, ritmo delicato e una sola parola target.
3. Generare il testo gratuitamente, correggerlo e approvarlo.
4. Generare lo storyboard gratuitamente, controllarlo scena per scena e
   approvarlo.
5. Confermare il costo musicale, accodare una sola variante e ascoltarla
   integralmente.
6. Verificare la reference ufficiale di Emma e caricare amici e mondo dell’episodio.
7. Confermare il costo residuo e accodare una sola volta render e QC.
8. Seguire il worker nella pagina episodio, quindi revisionare integralmente
   audio, video, Short, thumbnail e report.
9. Non pubblicare la thumbnail di concept: resta marcata `CONCEPT PREVIEW`.

Ogni fase è bloccata fino al completamento e all'approvazione della precedente.
Un QC non superato richiede revisione manuale e un nuovo episodio corretto:
la console non offre un rerender automatico che potrebbe duplicare la spesa.

YouTube è una fase successiva. Il token OAuth deve stare in un percorso
scrivibile del bucket montato, perché il client aggiorna il refresh token; non
va montato come file Secret Manager in sola lettura.

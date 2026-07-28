# Nuvibù Studio

![Banner Nuvibù](brand/nuvibu-youtube-banner-2560x1440.png)

Console autonoma per progettare, produrre, revisionare e misurare **canzoni e cartoni originali per un canale YouTube made-for-kids**. Il progetto non è collegato a British Institutes o Digital Campus.

L'identità pubblica di lavoro è **Nuvibù**. La direzione visiva corrente usa una mascotte 3D ricorrente, concept thumbnail ad alta densità e una Character Bible bloccata. Nome, handle e marchio devono comunque essere verificati prima del lancio pubblico.

## Stato del progetto

### Implementato

- brief episodio con età, tema, hook, parole target, durata, BPM e ritmo visivo;
- bozza originale di testo e metadati;
- varianti musicali e adattatore ElevenLabs Music;
- storyboard automatico in scene;
- adattatore Google Veo con character reference;
- modalità mock offline e senza crediti;
- montaggio FFmpeg 16:9;
- Short verticale 9:16;
- thumbnail, QC e approvazione umana;
- asset ledger con provider, modello, variante, costo e file;
- upload YouTube inizialmente privato e marcato `made for kids`;
- raccolta metriche e Growth Lab;
- SQLite locale, PostgreSQL/Neon in produzione;
- worker separato per i job lunghi;
- autenticazione Basic opzionale per la console privata.

### Da validare con gli account reali

- generazione musicale con la chiave del provider;
- generazione video con il progetto Google effettivo;
- consistenza del personaggio su un episodio completo;
- OAuth, upload e Analytics del canale YouTube;
- costi reali per episodio;
- disponibilità legale del nome Nuvibù.

## Direzione visiva

| Asset | Percorso |
|---|---|
| Avatar canale | `brand/nuvibu-avatar-800.png` |
| Banner YouTube | `brand/nuvibu-youtube-banner-2560x1440.png` |
| Key art mascotte | `brand/source/nuvibu-key-art.png` |
| Concept “Pulcini Arcobaleno” | `brand/concepts/pulcini-arcobaleno.png` |
| Concept “Cucù dietro la nuvola” | `brand/concepts/cucu-dietro-la-nuvola.png` |
| Character Bible | `data/character_bible_template.json` |

![Concept Pulcini Arcobaleno](brand/concepts/pulcini-arcobaleno.png)

La modalità mock usa queste immagini approvate per creare preview animate con pan e zoom. È un test della pipeline, **non un sostituto della generazione video live**.

## Pipeline

```text
Brief
  → testo e metadati
  → varianti musicali
  → storyboard
  → scene AI con character reference
  → video 16:9
  → Short 9:16 + thumbnail
  → QC automatico
  → approvazione umana
  → upload privato YouTube
  → revisione in YouTube Studio
  → Analytics e nuovo esperimento
```

## Avvio rapido senza chiavi API

Requisiti: Python 3.11+, FFmpeg e `pip`.

```bash
cp .env.example .env
python -m pip install -r requirements-dev.txt
python scripts/seed_demo.py --render
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Aprire `http://localhost:8000`.

Il mock genera musica strumentale originale e una preview in movimento da concept approvati. Ogni frame porta l'indicazione **PREVIEW TECNICA • NON PUBBLICARE**.

## Attivare i provider live

```dotenv
PROVIDER_MODE=live

ELEVENLABS_API_KEY=...
ELEVENLABS_MUSIC_MODEL=music_v2

GOOGLE_CLOUD_PROJECT=...
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_APPLICATION_CREDENTIALS=/percorso/service-account.json
VEO_OUTPUT_GCS_URI=gs://nome-bucket/nuvibu/
VEO_MODEL=veo-3.1-lite-generate-001
```

Le chiavi e i JSON di servizio non devono entrare nel repository. La cartella `secrets/` conserva soltanto `.gitkeep`.

### Guardrail costi

```dotenv
MAX_ESTIMATED_COST_USD_PER_EPISODE=40
MAX_MUSIC_VARIANTS=4
MAX_SCENE_RETRIES=2
```

La pipeline interrompe il job quando la stima supera il tetto configurato. I costi registrati vanno riconciliati con i pannelli dei provider.

## Collegare YouTube

1. Creare il canale dall'account Google proprietario.
2. Abilitare YouTube Data API e YouTube Analytics API.
3. Creare credenziali OAuth.
4. Salvare il client JSON come `secrets/youtube-client-secret.json`.
5. Eseguire:

```bash
python scripts/youtube_auth.py
```

6. Approvare l'episodio nella console.
7. Caricarlo come privato.
8. Controllare integralmente video, audio, thumbnail, audience e metadati in YouTube Studio.
9. Programmare o rendere pubblico manualmente.

## Worker

```bash
python scripts/run_worker.py
```

Una sola esecuzione:

```bash
python scripts/run_worker.py --once
```

Il worker incluso è adatto a una singola istanza. Per più worker serve una coda transazionale o un claim PostgreSQL con locking.

## Test

```bash
pytest
python -m compileall app scripts tests
```

I test coprono slug, storyboard, safety, prudenza del Growth Lab e pipeline mock completa con render, Short, thumbnail e report QC.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

## Struttura

```text
app/        FastAPI, UI, provider e pipeline
brand/      avatar, banner, key art e concept
data/       profilo canale, Character Bible e piano editoriale
 demo/      output tecnico portabile
 docs/      lancio, deployment e checklist
 scripts/   seed, worker, OAuth e derivazione asset
 tests/     test automatici
```

## Limiti dichiarati

- Il progetto non copia testi, melodie, personaggi, grafiche o titoli di altri canali.
- Nessun algoritmo garantisce visualizzazioni o monetizzazione.
- Il QC automatico verifica struttura e regole, non sostituisce la revisione umana.
- I concept inclusi definiscono la qualità visiva, ma la consistenza seriale richiede reference bloccate o un rig/modello dedicato.
- La pubblicazione automatica in pubblico resta disabilitata per scelta progettuale.

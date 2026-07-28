# Avvio rapido

Questo repository contiene **Nuvibù Studio**, la console standalone del canale YouTube Nuvibù.

## 1. Prova locale senza crediti

```bash
cp .env.example .env
python -m pip install -r requirements-dev.txt
python scripts/seed_demo.py --render
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Su Windows è disponibile `start-local.bat`.

## 2. Cosa vedrai

- dashboard e scheda episodio;
- testo, storyboard e metadati;
- varianti musicali mock;
- preview 16:9 e Short 9:16;
- thumbnail e report QC;
- Growth Lab.

Le preview mock usano concept di qualità ma non contengono vera animazione dei personaggi. Sono marcate **NON PUBBLICARE**.

## 3. Per produrre un episodio reale

Compilare `.env` con provider musicali e video, avviare il worker, caricare la reference 16:9 approvata e generare prima un test di 20–30 secondi. Solo dopo l'approvazione di voce, movimento e consistenza si estende il render all'episodio intero.

## 4. YouTube

La creazione del canale e il consenso OAuth devono provenire dall'account Google proprietario. Nuvibù Studio carica inizialmente i video come privati, così il passaggio in pubblico resta sotto controllo umano.

# Nuvibù Studio 0.4.1

Data: 1 agosto 2026

## Aggiornamenti

- Nuvibù definito come nome della piattaforma e del canale;
- sostituito il vecchio template lirico fisso con il motore editoriale v2;
- resi operativi sei format distinti: animali e versi, colori e trasformazioni, baby dance, cucù e sorpresa, storia musicale e nanna;
- aggiunta la selezione tra quattro concept con archetipi diversi prima della stesura finale;
- introdotta memoria del catalogo per titoli, ritornelli, verbi, rime, strutture, archetipi e gag;
- introdotte blacklist dinamica, similarità anti-riciclo e limite al riuso di frasi/rime degli ultimi dieci episodi;
- lo storyboard usa ora la progressione narrativa del concept selezionato;
- il QC valuta originalità, varietà dei verbi, progressione, metrica, forza del ritornello, chiarezza per età e coerenza testo/storyboard;
- un testo riciclato non può più ottenere 100/100 e viene fermato dal quality gate;
- introdotta **Emma & Friends**, con Emma protagonista obbligatoria di ogni scena;
- aggiunta la reference ufficiale di Emma ricavata dalle fotografie approvate;
- mantenuta la nuvola Nuvi come spalla visivamente secondaria;
- rigenerati key art, avatar e banner YouTube nella nuova gerarchia;
- aggiornata la Character Bible: volto, occhi, capelli, ciuffo e proporzioni di Emma sono immutabili;
- esteso il catalogo chiuso da dieci a diciotto look ufficiali precaricati, con Costumino Egeo, Abitino Cicladi e sei ulteriori outfit estivi;
- aggiunta la selezione tramite anteprime cliccabili: un solo look viene salvato e bloccato per l’intero episodio;
- impostato **Rosa confetto** come default dei nuovi episodi e **Classico Nuvibù** come fallback degli episodi precedenti privi di selezione;
- la copia specifica del look salvata con l’episodio viene inviata a Veo come immagine 1;
- il deploy Cloud Run rifiuta worktree non committati, usa il commit completo come tag immutabile e verifica l’hash di una thumbnail Emma dopo il rilascio;
- aggiornati i prompt Veo e la mapping delle reference: Emma, amici, mondo;
- preservata la compatibilità con i vecchi ruoli reference `nuvibu` e `cast`;
- la modalità mock usa concept approvati invece di facce geometriche;
- rinnovata la dashboard con l'identità Nuvibù;
- aggiunto workflow CI GitHub;
- mantenuto il blocco della pubblicazione pubblica automatica.

## Validazione locale

- `pytest`: test automatici;
- `python -m compileall`: controllo sintattico;
- render mock con H.264/AAC;
- Short verticale;
- thumbnail e report QC.

## Non validato senza credenziali del proprietario

- generazione reale musica;
- generazione reale Veo;
- OAuth, upload e Analytics YouTube;
- consistenza del personaggio su un episodio lungo;
- disponibilità legale del nome e degli handle.

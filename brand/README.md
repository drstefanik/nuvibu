# Identità visiva Nuvibù

Questa cartella contiene la direzione visiva corrente del canale.

## Asset principali

- `nuvibu-avatar-800.png`: avatar canale 800×800;
- `nuvibu-youtube-banner-2560x1440.png`: banner con elementi essenziali dentro la safe area centrale;
- `source/nuvibu-key-art.png`: key art principale con Emma in primo piano e Nuvi come spalla;
- `source/emma-looks/*.png`: dieci reference master approvate e bloccate di Emma;
- `source/emma-character-sheet.png`: alias di compatibilità per i flussi precedenti al catalogo;
- `source/nuvi-cloud-key-art.png`: reference storica della nuvola, ora personaggio secondario;
- `concepts/cucu-dietro-la-nuvola.png`: concept thumbnail episodio;
- `concepts/pulcini-arcobaleno.png`: concept thumbnail episodio;
- `concepts/nuvibu-soft-concept.png`: precedente esplorazione morbida, conservata solo come riferimento.

**Nuvibù è il nome della piattaforma e del canale, non il nome di un personaggio.** Emma è la protagonista ricorrente di “Emma & Friends”. Volto, occhi grigio-verdi, ciuffo alto, capelli castani e proporzioni da neonata sono immutabili nella Character Bible (`data/character_bible_template.json`). Nuvi, la nuvola, resta una piccola compagna e non può sostituire o dominare Emma.

## Catalogo look bloccato

L’abbigliamento non si modifica liberamente: per ogni episodio si sceglie
**esattamente uno** dei dieci look precaricati e quel look resta invariato in
tutte le scene, nei retry e negli asset derivati.

| ID | Nome mostrato |
|---|---|
| `emma-classic-nuvibu-v1` | Classico Nuvibù |
| `emma-pink-dress-v1` | Rosa confetto |
| `emma-lilac-overalls-v1` | Salopette lilla |
| `emma-sunshine-romper-v1` | Sole giallo |
| `emma-sky-sailor-v1` | Marinaretta cielo |
| `emma-mint-pinafore-v1` | Grembiulino menta |
| `emma-peach-rainbow-v1` | Arcobaleno pesca |
| `emma-starry-bedtime-v1` | Nanna stellata |
| `emma-coral-party-v1` | Festa corallo |
| `emma-cream-winter-v1` | Inverno crema |

I nuovi episodi partono da **Rosa confetto**. Gli episodi precedenti che non
hanno ancora una scelta salvata usano **Classico Nuvibù**, così il loro aspetto
non cambia implicitamente. L’editor seleziona il look cliccando la relativa
anteprima: non è previsto alcun upload arbitrario della reference di Emma.

Quando la scelta viene salvata, la piattaforma conserva con l’episodio una
copia specifica della reference selezionata. È quella copia, non un alias
globale modificabile, che viene inviata a Veo come **immagine 1**. Le reference
contengono le viste necessarie alla consistenza del personaggio. Prima della
produzione seriale lunga resta consigliato un rig 3D dedicato; le immagini
approvate alimentano nel frattempo il sistema di character consistency di Veo.

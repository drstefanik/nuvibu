from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, Sequence

from ..models import Episode


BASE_EDITORIAL_PROMPT = """
Scrivi una canzone originale per bambini, non una variazione di un modello
fisso. Seleziona un archetipo narrativo coerente con il format e costruisci
una micro-storia con apertura immediata, sviluppo, variazione e finale. Evita
formule generiche già utilizzate negli episodi precedenti. Il ritornello deve
essere specifico del personaggio o del tema e non sostituibile con quello di
un altro episodio. Ogni strofa deve introdurre un evento nuovo, non limitarsi
a nominare un oggetto o personaggio. Usa azioni visibili e sincronizzabili con
il video. Controlla metrica, accenti musicali, ripetizioni e cantabilità.
""".strip()

INITIAL_OVERUSE_WATCHLIST = (
    "guarda bene",
    "proprio così",
    "è qui",
    "brilla piano",
    "conta fino a tre",
    "cantalo con me",
    "una nuova sorpresa arriverà",
    "fai ciao con la manina",
)

FORMAT_ARCHETYPES: dict[str, tuple[str, ...]] = {
    "animali_e_versi": (
        "errore_e_correzione",
        "domanda_e_risposta",
        "indovinello",
        "personaggio_pasticcione",
        "canzone_cumulativa",
    ),
    "colori_e_trasformazioni": (
        "trasformazione_progressiva",
        "canzone_cumulativa",
        "conta_e_scopri",
        "mini_storia_con_finale",
        "indovinello",
    ),
    "baby_dance": (
        "ballo_a_comandi",
        "inseguimento_musicale",
        "canzone_cumulativa",
        "errore_e_correzione",
        "conta_e_scopri",
    ),
    "cucu_e_sorpresa": (
        "indovinello",
        "domanda_e_risposta",
        "conta_e_scopri",
        "mini_storia_con_finale",
        "personaggio_pasticcione",
    ),
    "storia_musicale": (
        "mini_storia_con_finale",
        "errore_e_correzione",
        "inseguimento_musicale",
        "personaggio_pasticcione",
        "canzone_cumulativa",
    ),
    "nanna": (
        "canzone_cumulativa",
        "mini_storia_con_finale",
        "domanda_e_risposta",
        "conta_e_scopri",
        "trasformazione_progressiva",
    ),
}

FORMAT_LABELS = {
    "animali_e_versi": "Animali e versi",
    "colori_e_trasformazioni": "Colori e trasformazioni",
    "baby_dance": "Baby dance",
    "cucu_e_sorpresa": "Cucù e sorpresa",
    "storia_musicale": "Storia musicale",
    "nanna": "Nanna",
}

ARCHETYPE_LABELS = {
    "domanda_e_risposta": "Domanda e risposta",
    "errore_e_correzione": "Errore e correzione",
    "canzone_cumulativa": "Canzone cumulativa",
    "conta_e_scopri": "Conta e scopri",
    "ballo_a_comandi": "Ballo a comandi",
    "inseguimento_musicale": "Inseguimento musicale",
    "trasformazione_progressiva": "Trasformazione progressiva",
    "indovinello": "Indovinello",
    "personaggio_pasticcione": "Personaggio pasticcione",
    "mini_storia_con_finale": "Mini-storia con finale",
}

ANIMAL_SOUNDS = {
    "cane": "bau bau",
    "cagnolino": "bau bau",
    "gatto": "miao miao",
    "gattino": "miao miao",
    "mucca": "muu muu",
    "pecora": "bee bee",
    "agnello": "bee bee",
    "gallina": "coccodè",
    "gallo": "chicchirichì",
    "pulcino": "pio pio",
    "pulcini": "pio pio",
    "papera": "qua qua",
    "anatra": "qua qua",
    "maiale": "oink oink",
    "rana": "cra cra",
    "gufo": "uh uh",
    "leone": "roar",
    "pappagallo": "cra cra",
    "pappì": "cra cra",
    "pappi": "cra cra",
}

KNOWN_VERBS = (
    "apre",
    "aspetta",
    "balla",
    "batte",
    "cerca",
    "chiude",
    "corre",
    "dondola",
    "gira",
    "guarda",
    "indica",
    "mescola",
    "nasconde",
    "prova",
    "raccoglie",
    "ride",
    "salta",
    "scivola",
    "spinge",
    "spunta",
    "tocca",
    "vola",
)

SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")
WORD_RE = re.compile(r"[a-zà-öø-ÿ0-9']+", re.IGNORECASE)


@dataclass(slots=True)
class CatalogMemory:
    recent_titles: list[str] = field(default_factory=list)
    recent_refrains: list[str] = field(default_factory=list)
    recent_verbs: list[str] = field(default_factory=list)
    recent_rhyme_pairs: list[tuple[str, str]] = field(default_factory=list)
    recent_structures: list[tuple[str, ...]] = field(default_factory=list)
    recent_characters: list[str] = field(default_factory=list)
    recent_archetypes: list[str] = field(default_factory=list)
    recent_gags: list[str] = field(default_factory=list)
    recent_action_sequences: list[list[str]] = field(default_factory=list)
    recent_lyrics: list[str] = field(default_factory=list)
    blocked_phrases: list[str] = field(default_factory=list)
    catalog_size: int = 0

    def prompt_payload(self) -> dict:
        return {
            "ultimi_titoli": self.recent_titles[:10],
            "ritornelli_recenti": self.recent_refrains[:10],
            "verbi_recenti": self.recent_verbs[:40],
            "rime_recenti": [
                " / ".join(pair) for pair in self.recent_rhyme_pairs[:30]
            ],
            "strutture_recenti": [
                list(structure) for structure in self.recent_structures[:10]
            ],
            "personaggi_recenti": self.recent_characters[:40],
            "archetipi_recenti": self.recent_archetypes[:10],
            "gag_recenti": self.recent_gags[:10],
            "sequenze_azioni_recenti": self.recent_action_sequences[:10],
            "frasi_bloccate_ora": self.blocked_phrases,
        }


@dataclass(slots=True)
class SongCandidate:
    archetype: str
    concept: str
    gag: str
    progression: list[str]
    lyrics: str
    originality: int = 0
    singability: int = 0
    energy: int = 0
    coherence: int = 0
    final_score: int = 0
    rejected: bool = False
    rejection_reasons: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "archetype": self.archetype,
            "archetype_label": ARCHETYPE_LABELS[self.archetype],
            "concept": self.concept,
            "gag": self.gag,
            "scores": {
                "originality": self.originality,
                "singability": self.singability,
                "energy": self.energy,
                "coherence": self.coherence,
                "total": self.final_score,
            },
            "rejected": self.rejected,
            "rejection_reasons": self.rejection_reasons,
        }


@dataclass(slots=True)
class SongGeneration:
    lyrics: str
    song_format: str
    selected: SongCandidate
    candidates: list[SongCandidate]
    memory: CatalogMemory

    def diagnostics(self) -> dict:
        return {
            "engine_version": 2,
            "base_instruction": BASE_EDITORIAL_PROMPT,
            "format": self.song_format,
            "format_label": FORMAT_LABELS[self.song_format],
            "archetype": self.selected.archetype,
            "archetype_label": ARCHETYPE_LABELS[self.selected.archetype],
            "concept": self.selected.concept,
            "gag": self.selected.gag,
            "progression": self.selected.progression,
            "selected_scores": self.selected.summary()["scores"],
            "candidates": [candidate.summary() for candidate in self.candidates],
            "memory": self.memory.prompt_payload(),
        }


def _plain(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in value if not unicodedata.combining(character))


def _normalized_text(value: str) -> str:
    return " ".join(WORD_RE.findall(_plain(value)))


def _normalized_lines(lyrics: str) -> list[str]:
    return [
        _normalized_text(line)
        for line in lyrics.splitlines()
        if line.strip() and not SECTION_RE.match(line.strip())
    ]


def _sections(lyrics: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    name = "Canzone"
    lines: list[str] = []
    for raw_line in lyrics.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = SECTION_RE.match(line)
        if heading:
            if lines:
                sections.append((name, lines))
            name = heading.group(1).strip()
            lines = []
        else:
            lines.append(line)
    if lines:
        sections.append((name, lines))
    return sections


def _refrains(lyrics: str) -> list[str]:
    return [
        "\n".join(lines)
        for name, lines in _sections(lyrics)
        if "ritornello" in _plain(name) or "chorus" in _plain(name)
    ]


def _structure(lyrics: str) -> tuple[str, ...]:
    return tuple(_plain(name) for name, _lines in _sections(lyrics))


def _verbs(lyrics: str) -> list[str]:
    normalized = f" {_normalized_text(lyrics)} "
    return [
        verb
        for verb in KNOWN_VERBS
        if f" {verb} " in normalized
    ]


def _last_word(line: str) -> str:
    words = WORD_RE.findall(_plain(line))
    return words[-1] if words else ""


def _rhyme_key(word: str) -> str:
    return word[-3:] if len(word) >= 3 else word


def _rhyme_pairs(lyrics: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for _name, lines in _sections(lyrics):
        for left, right in zip(lines, lines[1:]):
            left_word = _last_word(left)
            right_word = _last_word(right)
            if left_word and right_word:
                pairs.append((_rhyme_key(left_word), _rhyme_key(right_word)))
    return pairs


def _episode_generation(episode: Episode) -> dict:
    generation = (episode.concept_json or {}).get("editorial_generation")
    return generation if isinstance(generation, dict) else {}


def build_catalog_memory(
    recent_episodes: Iterable[Episode] = (),
    catalog_episodes: Iterable[Episode] | None = None,
) -> CatalogMemory:
    recent = [episode for episode in recent_episodes if episode.lyrics_text][:10]
    catalog = [
        episode
        for episode in (catalog_episodes if catalog_episodes is not None else recent)
        if episode.lyrics_text
    ]
    recent_lyrics = [episode.lyrics_text or "" for episode in recent]
    phrase_counts = Counter(
        phrase
        for episode in catalog
        for phrase in INITIAL_OVERUSE_WATCHLIST
        if _normalized_text(phrase)
        in _normalized_text(episode.lyrics_text or "")
    )
    latest = _normalized_text(recent_lyrics[0]) if recent_lyrics else ""
    blocked = [
        phrase
        for phrase in INITIAL_OVERUSE_WATCHLIST
        if _normalized_text(phrase) in latest
        or (
            catalog
            and phrase_counts[phrase] / len(catalog) > 0.15
        )
    ]
    generations = [_episode_generation(episode) for episode in recent]
    return CatalogMemory(
        recent_titles=[episode.title for episode in recent],
        recent_refrains=[
            refrain
            for lyrics in recent_lyrics
            for refrain in _refrains(lyrics)[:1]
        ],
        recent_verbs=[
            verb for lyrics in recent_lyrics for verb in _verbs(lyrics)
        ],
        recent_rhyme_pairs=[
            pair for lyrics in recent_lyrics for pair in _rhyme_pairs(lyrics)
        ],
        recent_structures=[_structure(lyrics) for lyrics in recent_lyrics],
        recent_characters=[
            str(character)
            for episode in recent
            for character in episode.featured_characters
            if _plain(str(character)) not in {"emma", "nuvibu"}
        ],
        recent_archetypes=[
            str(generation.get("archetype"))
            for generation in generations
            if generation.get("archetype")
        ],
        recent_gags=[
            str(generation.get("gag"))
            for generation in generations
            if generation.get("gag")
        ],
        recent_action_sequences=[
            [
                str(action)
                for action in generation.get("progression", [])
                if str(action).strip()
            ]
            for generation in generations
            if isinstance(generation.get("progression"), list)
        ],
        recent_lyrics=recent_lyrics,
        blocked_phrases=blocked,
        catalog_size=len(catalog),
    )


def resolve_song_format(episode: Episode) -> str:
    brief = _plain(
        " ".join(
            [
                episode.theme,
                episode.title,
                episode.hook,
                *episode.target_words,
                *episode.featured_characters,
            ]
        )
    )
    explicit = _plain(episode.theme)
    if "nanna" in explicit:
        return "nanna"
    if "animal" in explicit or "vers" in explicit:
        return "animali_e_versi"
    if "color" in explicit or "trasform" in explicit:
        return "colori_e_trasformazioni"
    if "dance" in explicit or "ball" in explicit or "moviment" in explicit:
        return "baby_dance"
    if "cucu" in explicit or "sorpres" in explicit:
        return "cucu_e_sorpresa"
    if "storia" in explicit:
        return "storia_musicale"
    if any(
        token in brief for token in ("dormi", "sonno", "stelline", "culla")
    ):
        return "nanna"
    if any(
        token in brief
        for token in (
            "gatto",
            "cane",
            "mucca",
            "pecora",
            "gallina",
            "pulcin",
            "pappagall",
            "fattoria",
        )
    ):
        return "animali_e_versi"
    if "color" in brief or "trasform" in brief:
        return "colori_e_trasformazioni"
    if "dance" in brief or "ball" in brief or "moviment" in brief:
        return "baby_dance"
    if "cucu" in brief or "sorpres" in brief or "nascond" in brief:
        return "cucu_e_sorpresa"
    return "storia_musicale"


def _supporting_characters(episode: Episode) -> list[str]:
    supporting: list[str] = []
    for raw_name in episode.featured_characters:
        name = str(raw_name).strip()
        normalized = _plain(name)
        if not name or normalized in {
            "emma",
            "nuvibu",
            "nuvi",
            "nuvi la nuvola",
        }:
            continue
        if name not in supporting:
            supporting.append(name)
    return supporting


def _targets(episode: Episode, fallback: Sequence[str]) -> list[str]:
    values = [str(value).strip() for value in episode.target_words if str(value).strip()]
    return (values or list(fallback))[:5]


def _subject(episode: Episode, fallback: str) -> str:
    characters = _supporting_characters(episode)
    return characters[0] if characters else _targets(episode, [fallback])[0]


def _animal_sound(subject: str, targets: Sequence[str]) -> str:
    subject_text = _plain(subject)
    for animal, sound in ANIMAL_SOUNDS.items():
        if _plain(animal) in subject_text:
            return sound
    target_text = _plain(" ".join(targets))
    for animal, sound in ANIMAL_SOUNDS.items():
        if _plain(animal) in target_text:
            return sound
    return "miao miao"


def _other_sound(correct: str, offset: int = 0) -> str:
    choices = ["bau bau", "muu muu", "pio pio", "qua qua", "cra cra"]
    for choice in choices[offset:] + choices[:offset]:
        if choice != correct:
            return choice
    return "din don"


def _duration_sections(
    intro: list[str],
    verse_one: list[str],
    chorus: list[str],
    verse_two: list[str],
    finale: list[str],
    *,
    duration_seconds: int,
    bridge: list[str] | None = None,
) -> str:
    sections: list[tuple[str, list[str]]]
    if duration_seconds > 45:
        sections = [
            ("Intro", intro),
            ("Ritornello", chorus),
            ("Strofa 1", verse_one),
            ("Ritornello", chorus),
            ("Strofa 2", verse_two),
            ("Ritornello finale", finale),
        ]
    else:
        sections = [
            ("Intro", intro),
            ("Strofa 1", verse_one),
            ("Ritornello", chorus),
            ("Ritornello finale", finale),
        ]
    if duration_seconds > 90 and bridge:
        sections.insert(-1, ("Ponte", bridge))
    return "\n\n".join(
        f"[{name}]\n" + "\n".join(line.strip() for line in lines)
        for name, lines in sections
    )


def _animal_song(
    episode: Episode,
    archetype: str,
) -> tuple[str, str, list[str], str]:
    targets = _targets(episode, ["gatto", "cane", "mucca"])
    subject = _subject(episode, targets[0])
    sound = _animal_sound(subject, targets)
    wrong_one = _other_sound(sound)
    wrong_two = _other_sound(sound, 2)
    animal_two = targets[1] if len(targets) > 1 else "gattino"
    animal_two_sound = _animal_sound(animal_two, [animal_two])
    if archetype == "errore_e_correzione":
        concept = f"{subject} confonde due versi prima di trovare il suo"
        gag = f"{subject} prova {wrong_one} e {wrong_two} al posto di {sound}"
        progression = [
            f"{subject} apre il becco e fa il verso sbagliato {wrong_one}",
            "Emma reagisce ridendo e invita a riprovare",
            f"{subject} tenta un secondo verso sbagliato {wrong_two}",
            f"Emma mostra il ritmo e {subject} trova il verso {sound}",
            "Tutti gli animali rispondono con il proprio verso",
        ]
        lyrics = _duration_sections(
            [f"{subject} apre il becco: «{wrong_one}!»", "Emma sgrana gli occhi: «Oh-oh!»"],
            [
                f"«Sei un cagnolino?» chiede Emma ridendo,",
                f"{subject} scuote le piume e riprova correndo.",
                f"Dal recinto risponde davvero «{wrong_one}»,",
                f"ma la voce di {subject} non è questa qua.",
            ],
            [
                f"{subject}, qual è la voce che hai?",
                f"Non {wrong_one}, non {wrong_two}: dai!",
                "Petto in fuori, un respiro e poi…",
                f"«{sound}!», adesso sì che puoi!",
            ],
            [
                f"Secondo tentativo: parte «{wrong_two}!»",
                "Emma fa una pausa, il cortile sta zitto.",
                f"{subject} ascolta il cuore: tum, tum, tum,",
                f"poi libera la voce: «{sound}!»",
            ],
            [
                f"{subject}, questa voce è proprio tua:",
                f"«{sound}!» rimbalza per la via.",
                "Emma batte il tempo, gli amici fanno coro,",
                f"{subject} canta forte: ha trovato il suo tesoro!",
            ],
            duration_seconds=episode.duration_seconds,
            bridge=[
                "Ora ogni amico entra al segnale,",
                "una voce diversa, un coro animale.",
            ],
        )
        return concept, gag, progression, lyrics

    if archetype == "domanda_e_risposta":
        concept = f"Emma chiama {subject} e il cortile risponde a ritmo"
        gag = "Una voce arriva dal posto sbagliato e confonde Emma"
        progression = [
            "Emma sente un verso dietro il fienile",
            f"Emma domanda chi fa {sound}",
            f"{subject} risponde e compare",
            f"{animal_two} aggiunge un verso diverso",
            "Il cast costruisce un coro a chiamata e risposta",
        ]
        lyrics = _duration_sections(
            [f"Dal fienile rimbalza «{sound}!»", "Emma tende l’orecchio: chi sarà?"],
            [
                f"«Chi fa {sound} dietro il portone?»",
                f"{subject} salta fuori dal covone.",
                "Emma chiama piano, poi chiama più forte,",
                "la risposta fa ballare anche le porte.",
            ],
            [
                f"Emma: «{subject}, fammi sentire!»",
                f"{subject}: «{sound}», pronto a partire!",
                f"Emma: «Ancora!» — «{sound}!»",
                "Domanda e risposta, eccoci qua!",
            ],
            [
                f"Poi si sente {animal_two_sound} vicino al mulino,",
                f"arriva {animal_two} lungo il sentierino.",
                "Una voce alla volta si unisce al gioco,",
                "il coro della fattoria cresce poco a poco.",
            ],
            [
                f"Emma chiama {subject}: «Fammi sentire!»",
                f"«{sound}!», fa tutto il cortile.",
                f"{animal_two} risponde, poi tocca a Emma:",
                "ogni voce diversa completa la festa!",
            ],
            duration_seconds=episode.duration_seconds,
        )
        return concept, gag, progression, lyrics

    if archetype == "indovinello":
        concept = f"Emma riconosce {subject} soltanto dal suo verso"
        gag = f"Spuntano prima coda e zampe, ma la voce {sound} svela tutto"
        progression = [
            "Una sagoma resta nascosta",
            "Compare soltanto una piccola parte del corpo",
            f"Il personaggio fa {sound}",
            f"Emma indovina {subject}",
            "La sagoma si rivela e guida il coro",
        ]
        lyrics = _duration_sections(
            ["Una coda sbuca dietro il granaio,", "due zampette fanno tic tac sul solaio."],
            [
                "Ha due occhi vispi e un passo leggero,",
                "ma resta nascosto dietro il sentiero.",
                f"Fa «{sound}» e poi tace di colpo:",
                "Emma ha già capito chi c’è là sotto.",
            ],
            [
                "Chi ha lasciato quelle impronte?",
                f"Chi fa «{sound}» dietro il ponte?",
                f"È {subject}, la risposta è questa!",
                "Salta fuori e comincia la festa.",
            ],
            [
                f"{animal_two} lascia un’altra traccia,",
                "Emma la segue con un sorriso in faccia.",
                f"Un «{animal_two_sound}» scioglie l’indovinello,",
                "due nuovi amici danzano nel cortile bello.",
            ],
            [
                "Ora le impronte vanno tutte al ponte,",
                f"{subject} fa «{sound}» proprio di fronte.",
                f"{animal_two} risponde «{animal_two_sound}» in allegria:",
                "Emma ha risolto tutta la compagnia!",
            ],
            duration_seconds=episode.duration_seconds,
        )
        return concept, gag, progression, lyrics

    if archetype == "personaggio_pasticcione":
        concept = f"{subject} perde il ritmo e mette sottosopra il coro"
        gag = f"Ogni volta che dovrebbe fare {sound}, {subject} parte in ritardo"
        progression = [
            "Gli animali preparano il coro",
            f"{subject} entra in anticipo con {sound}",
            "Emma ferma il coro e mostra il segnale",
            f"{subject} entra in ritardo e poi recupera",
            "Il finale riesce con un buffo ultimo colpo",
        ]
        lyrics = _duration_sections(
            ["Il coro è pronto, Emma alza un dito,", f"{subject} parte prima: «{sound}!»"],
            [
                "La mucca fa pausa, la papera aspetta,",
                f"ma {subject} ha una voce troppo frettolosa.",
                "Emma abbassa il dito: silenzio totale,",
                "poi disegna nell’aria il segnale.",
            ],
            [
                "Aspetta… aspetta… adesso vai!",
                f"{subject} fa «{sound}» e non sbaglia mai.",
                "Uno entra, l’altro risponderà:",
                "il coro pasticcione finalmente partirà!",
            ],
            [
                f"{subject} questa volta rimane in silenzio,",
                "controlla il segnale con grande impegno.",
                "Poi arriva tardi, ma Emma gira il tempo,",
                "e il buffo ritardo diventa un accento.",
            ],
            [
                "Aspetta… aspetta… adesso vai!",
                f"«{sound}!», e il cortile salta assai.",
                f"{subject} fa l’ultimo verso da solista:",
                "Emma ride: che perfetta svista!",
            ],
            duration_seconds=episode.duration_seconds,
        )
        return concept, gag, progression, lyrics

    concept = "Ogni animale aggiunge una voce al coro di Emma"
    gag = f"{subject} prova a tenere tutte le voci e resta senza fiato"
    progression = [
        f"{subject} apre il coro con {sound}",
        f"{animal_two} aggiunge {animal_two_sound}",
        "Emma ripete entrambe le voci in sequenza",
        "Un terzo animale aggiunge un nuovo suono",
        "Tutto il cast esegue la sequenza completa",
    ]
    lyrics = _duration_sections(
        [f"{subject} dà il via con «{sound}!»", "Emma raccoglie la voce e la porta più in là."],
        [
            f"Prima canta {subject}: «{sound}!»",
            f"poi {animal_two} risponde: «{animal_two_sound}!»",
            "Emma tiene le voci dentro le mani,",
            "le rilancia insieme, vicine e lontane.",
        ],
        [
            f"Prima «{sound}», poi «{animal_two_sound}»,",
            "una voce nuova si aggiungerà.",
            "Emma fa il segnale, nessuno si perde,",
            "il coro cresce e il cortile si accende.",
        ],
        [
            "Arriva una rana con un piccolo «cra»,",
            "la sequenza si allunga e riparte da qua.",
            f"«{sound}», «{animal_two_sound}», «cra cra» in fila,",
            f"{subject} prende fiato e poi ci riprova.",
        ],
        [
            f"«{sound}», «{animal_two_sound}», «cra cra»!",
            "Tutta la fattoria risponderà.",
            "Emma chiude il coro con un battito solo,",
            "le voci fanno festa e poi prendono il volo.",
        ],
        duration_seconds=episode.duration_seconds,
    )
    return concept, gag, progression, lyrics


def _color_song(
    episode: Episode,
    archetype: str,
) -> tuple[str, str, list[str], str]:
    colors = _targets(episode, ["rosso", "giallo", "blu", "verde"])
    first = colors[0]
    second = colors[1] if len(colors) > 1 else "giallo"
    third = colors[2] if len(colors) > 2 else "blu"
    subject = _subject(episode, "palloncino")
    if archetype == "trasformazione_progressiva":
        concept = f"{subject} assorbe un colore a ogni salto e trasforma il mondo"
        gag = f"Una goccia di {first} atterra sul naso di Emma"
        progression = [
            f"Una goccia {first} colora un dettaglio di {subject}",
            f"Il secondo salto aggiunge {second}",
            f"Il terzo salto aggiunge {third}",
            "I colori si mescolano e cambiano lo scenario",
            "Il mondo intero completa l’arcobaleno",
        ]
        lyrics = _duration_sections(
            [f"Plin! Una goccia {first} cade sul cappello,", "Emma la segue lungo un ruscello."],
            [
                f"Il primo salto dipinge {subject} di {first},",
                f"il secondo porta una scia di {second}.",
                f"{third} fa una curva e sfiora il naso,",
                "Emma ride forte per quel buffo caso.",
            ],
            [
                f"{first} sulle punte, {second} più su,",
                f"una giravolta e compare {third}.",
                f"{subject} cambia a ogni salto di Emma:",
                "colore dopo colore si trasforma la scena!",
            ],
            [
                f"{first} e {second} si incontrano al centro,",
                f"{third} corre in tondo e poi salta dentro.",
                "La strada, le nuvole, persino il mulino",
                "indossano insieme un vestito nuovo e carino.",
            ],
            [
                f"{first} sulle punte, {second} più su,",
                f"una giravolta e ritorna {third}.",
                f"{subject} apre un arco sopra Emma:",
                "tutti i colori hanno cambiato la scena!",
            ],
            duration_seconds=episode.duration_seconds,
            bridge=[
                "Una goccia da sola colora un pezzetto,",
                "tutte le gocce insieme cambiano ogni oggetto.",
            ],
        )
    elif archetype == "canzone_cumulativa":
        concept = f"Emma raccoglie {first}, {second} e {third} in una sequenza crescente"
        gag = "Il colore più piccolo prova a superare tutti e schizza fuori dal barattolo"
        progression = [
            f"Emma raccoglie il colore {first}",
            f"Aggiunge {second} senza perdere {first}",
            f"Aggiunge {third} e ripete la sequenza",
            "La sequenza di colori accelera",
            "Tutti i colori formano un arco completo",
        ]
        lyrics = _duration_sections(
            [f"Rotola un barattolo {first} fino a Emma,", "si apre con un plop e colora una gemma."],
            [
                f"Emma prende {first} e lo mette nel cestino,",
                f"arriva {second} lungo un nastro piccolino.",
                f"Prima {first}, poi {second}: la fila crescerà,",
                f"{third} schizza fuori e davanti passerà!",
            ],
            [
                f"Prima {first}, poi {second},",
                f"aggiungi {third} mentre gira il mondo.",
                "Tieni la sequenza, non lasciarla andare:",
                f"{subject} la ripete e la fa brillare!",
            ],
            [
                f"{first} apre la strada, {second} viene poi,",
                f"{third} fa una curva e ritorna tra noi.",
                "Emma riparte senza perdere un colore,",
                "la fila diventa un arco sempre maggiore.",
            ],
            [
                f"Prima {first}, poi {second},",
                f"chiude {third} e si accende il mondo.",
                f"{subject} lancia la sequenza sopra la città:",
                "l’arcobaleno intero non si perderà!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    elif archetype == "conta_e_scopri":
        concept = "Tre tocchi rivelano tre colori e una quarta trasformazione"
        gag = "Al terzo tocco sembra non accadere nulla, poi il colore esplode alle spalle"
        progression = [
            f"Il primo tocco rivela {first}",
            f"Il secondo tocco rivela {second}",
            "Il terzo tocco sembra fallire",
            f"{third} compare a sorpresa dietro Emma",
            "I tre tocchi attivano la trasformazione finale",
        ]
        lyrics = _duration_sections(
            ["Tre bottoni dormono sopra una parete,", "Emma alza un dito: che cosa vedrete?"],
            [
                f"Uno: il primo bottone spruzza {first},",
                f"due: il secondo lascia una traccia {second}.",
                "Tre: nessun colore, soltanto un rumorino…",
                f"poi {third} esplode alle spalle del pulcino!",
            ],
            [
                f"Uno apre {first}, due porta {second},",
                f"tre libera {third} e trasforma il mondo.",
                "Tocca nello stesso ordine insieme ad Emma:",
                "tre piccoli colpi, una grandissima scena!",
            ],
            [
                "Emma ripete i tocchi, ma cambia velocità,",
                "il muro si arrotola e una porta apparirà.",
                f"Dietro la porta {first}, {second} e {third}",
                "dipingono una scala che sale sempre più.",
            ],
            [
                f"Uno apre {first}, due porta {second},",
                f"tre libera {third} sopra tutto il mondo.",
                f"{subject} sale la scala e saluta Emma:",
                "l’ultimo gradino completa la scena!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    elif archetype == "indovinello":
        concept = "Emma indovina i colori dagli effetti che lasciano sugli oggetti"
        gag = f"Il colore {second} tinge per sbaglio soltanto un calzino"
        progression = [
            "Compare una traccia colorata senza sorgente",
            "Emma segue la traccia e osserva un oggetto trasformato",
            "Un secondo indizio restringe la scelta",
            f"Emma indovina {first}",
            "Il colore si rivela e trasforma tutto",
        ]
        lyrics = _duration_sections(
            ["Una traccia corre fuori da un cassetto,", "ma il colore resta nascosto dietro un oggetto."],
            [
                "Scalda il cappello e colora una mela,",
                "lascia una riga sopra la vela.",
                f"Emma pensa, poi esclama: «È {first}!»",
                "la traccia fa un salto e le tinge il naso.",
            ],
            [
                "Quale colore ha lasciato la pista?",
                f"È {first}, la risposta è già in vista.",
                f"{second} sul calzino, {third} sopra il tamburo:",
                "segui ogni indizio e il colore è sicuro!",
            ],
            [
                f"Un nuovo segno {second} passa sul pavimento,",
                f"ma dipinge un calzino e scappa con il vento.",
                f"{third} lascia stelline attorno a {subject},",
                "Emma risolve anche il secondo perché.",
            ],
            [
                f"{first}, {second}, {third}: ogni pista è in vista,",
                "Emma ha trovato ciascun artista.",
                f"I colori escono insieme dietro {subject}:",
                "la stanza si trasforma dal soffitto fin quaggiù!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    else:
        concept = f"{subject} perde i colori ed Emma li riporta a casa uno alla volta"
        gag = f"{third} scappa sempre un passo più avanti"
        progression = [
            f"{subject} si sveglia senza colori",
            f"Emma recupera {first}",
            f"Un tentativo con {second} trasforma l’oggetto sbagliato",
            f"Emma raggiunge {third}",
            "I colori tornano insieme e creano una nuova figura",
        ]
        lyrics = _duration_sections(
            [f"Oh! {subject} stamattina è tutto bianco,", "tre colori sono fuggiti lasciandolo stanco."],
            [
                f"Emma trova {first} sotto un grande fiore,",
                f"prova con {second}, ma colora l’ascensore.",
                f"{third} corre avanti con un nastro svolazzante,",
                f"{subject} lo rincorre, ma resta distante.",
            ],
            [
                f"Torna {first}, torna {second},",
                f"aspetta {third}, non scappare dal mondo!",
                f"Ogni colore ha un posto su {subject}:",
                "Emma li riunisce e il disegno torna su.",
            ],
            [
                f"Emma tende un ponte fatto di {first},",
                f"{third} si ferma e si siede in quel caso.",
                f"{second} torna indietro dal buffo ascensore,",
                f"{subject} ritrova finalmente ogni colore.",
            ],
            [
                f"Ecco {first}, ecco {second},",
                f"insieme a {third} fanno festa nel mondo.",
                f"{subject} non è uguale a com’era stamattina:",
                "ora porta un arcobaleno sulla schiena!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    return concept, gag, progression, lyrics


def _dance_song(
    episode: Episode,
    archetype: str,
) -> tuple[str, str, list[str], str]:
    subject = _subject(episode, "Emma")
    moves = _targets(episode, ["batti", "salta", "gira", "fermo"])
    first, second, third = (moves + ["salta", "gira"])[:3]
    concepts = {
        "ballo_a_comandi": "Tre comandi diventano una coreografia riconoscibile",
        "inseguimento_musicale": f"Emma insegue {subject} copiando una mossa nuova a ogni tappa",
        "canzone_cumulativa": "Ogni giro aggiunge una mossa senza perdere le precedenti",
        "errore_e_correzione": f"{subject} scambia i comandi e inventa una mossa buffa",
        "conta_e_scopri": "Quattro segnali numerati sbloccano il ballo finale",
    }
    concept = concepts[archetype]
    if archetype == "ballo_a_comandi":
        gag = f"Al comando «{second}», {subject} fa «{third}» e resta in posa"
        progression = [
            f"Emma mostra il comando {first}",
            f"Il cast risponde con {second}",
            f"{subject} sbaglia il comando e crea una gag",
            f"Emma aggiunge {third} alla sequenza",
            "Tutto il cast esegue la coreografia completa più veloce",
        ]
        lyrics = _duration_sections(
            ["Il tamburo fa bum: piedi pronti!", "Emma dà il segnale, si parte insieme."],
            [
                f"Quando Emma dice «{first}», le mani fanno clap,",
                f"quando Emma dice «{second}», i piedi fanno tap.",
                f"Ma {subject} sente «{third}» e gira al contrario:",
                "nasce una mossa buffa, fuori dal vocabolario!",
            ],
            [
                f"{first}! Poi {second}! Adesso {third}!",
                "Fermo un battito… e riparti tu.",
                "Mani, piedi, giro e stop:",
                "questa è la danza che non scambi più!",
            ],
            [
                f"Prima {first}, senza perdere il passo,",
                f"aggiungi {second}, poi scendi un po’ in basso.",
                f"Con {third} la sequenza diventa completa,",
                "Emma la rilancia più allegra e più svelta.",
            ],
            [
                f"{first}! Poi {second}! Adesso {third}!",
                "Fermo un battito… e riparti tu.",
                f"{subject} fa la mossa buffa nel gran finale,",
                "tutto il gruppo la copia: è il nostro segnale!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    elif archetype == "inseguimento_musicale":
        gag = f"{subject} lascia una scarpa a metà del percorso"
        progression = [
            f"{subject} parte danzando e invita Emma a seguirlo",
            f"Emma supera la tappa {first}",
            f"La pista cambia con la mossa {second}",
            f"Una scarpa perduta suggerisce {third}",
            "Emma raggiunge il personaggio e chiude il ballo",
        ]
        lyrics = _duration_sections(
            [f"{subject} scappa a ritmo: tip tap tà!", "Emma segue le impronte che lascia qua e là."],
            [
                f"Prima impronta: {first} davanti al portone,",
                f"seconda impronta: {second} sopra un pallone.",
                f"{subject} curva a destra e perde una scarpetta,",
                "Emma copia il passo e la raggiunge in fretta.",
            ],
            [
                f"Segui {subject}: {first} e vai,",
                f"passa a {second}, non fermarti mai.",
                f"Trova la scarpa, fai {third} laggiù:",
                "una mossa dopo l’altra lo raggiungi tu!",
            ],
            [
                f"La pista sale, {subject} cambia direzione,",
                f"Emma fa {third} sopra un grande bottone.",
                "Le impronte si fermano dietro al tamburo,",
                "l’ultimo passo li riunisce di sicuro.",
            ],
            [
                f"Emma e {subject}: {first} e vai,",
                f"poi {second}, fianco a fianco ormai.",
                f"Scarpa ritrovata, fai {third} laggiù:",
                "il ballo dell’inseguimento lo guidi tu!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    elif archetype == "canzone_cumulativa":
        gag = f"{subject} ricorda tutte le mosse ma dimentica lo stop"
        progression = [
            f"La sequenza parte con {first}",
            f"Si aggiunge {second}",
            f"Si aggiunge {third}",
            f"{subject} ripete tutto senza fermarsi allo stop",
            "Il gruppo completa e chiude la sequenza",
        ]
        lyrics = _duration_sections(
            [f"Una mossa entra nel cerchio: {first}!", "Emma la prende e la passa agli amici."],
            [
                f"Comincia con {first}, poi aggiungi {second},",
                "due mosse viaggiano intorno al mondo.",
                f"{subject} le ripete e ne chiede di più,",
                f"Emma mette {third} proprio alla fine, laggiù.",
            ],
            [
                f"{first}, {second}, {third} e stop!",
                "Ripeti la fila dal basso al top.",
                "Una mossa si aggiunge, nessuna va via:",
                "questa è la nostra danza in compagnia!",
            ],
            [
                f"{subject} fa {first}, {second}, {third},",
                "ma passa lo stop e continua di più.",
                "Emma chiude il cerchio battendo un piedino,",
                "la sequenza si ferma con un inchino.",
            ],
            [
                f"{first}, {second}, {third} e stop!",
                f"{subject} si ferma: perfetto, top!",
                "Tutte le mosse tornano in fila:",
                "Emma dà il cinque e il tamburo scintilla!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    elif archetype == "errore_e_correzione":
        gag = f"{subject} scambia {second} con {third} per due volte"
        progression = [
            f"Emma insegna {first}",
            f"{subject} esegue correttamente la prima mossa",
            f"Il personaggio confonde {second} con {third}",
            "Emma rallenta il comando e mostra la differenza",
            "Il personaggio corregge la sequenza nel finale",
        ]
        lyrics = _duration_sections(
            [f"Emma dice «{first}»: parte il ballo,", f"{subject} lo copia senza fare uno sbaglio."],
            [
                f"Poi Emma chiama «{second}», chiaro e sonoro,",
                f"ma {subject} fa {third} e salta fuori dal coro.",
                "Emma rallenta e mostra la direzione,",
                "una mossa alla volta risolve la confusione.",
            ],
            [
                f"Non {third}: adesso {second}!",
                f"Poi torna {first} e riprendi il girotondo.",
                "Ascolta il comando, lascia un battito in più:",
                f"{subject} fa la mossa giusta e la scegli anche tu!",
            ],
            [
                f"Seconda prova: {first}, {second}, stop,",
                f"{subject} quasi gira, ma si ferma al toc.",
                f"Arriva {third} soltanto al suo momento,",
                "la correzione diventa un nuovo movimento.",
            ],
            [
                f"Prima {first}, adesso {second},",
                f"alla fine {third} e si accende il mondo.",
                f"{subject} non scambia più la direzione:",
                "Emma chiude il ballo con la sua correzione!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    else:
        gag = f"Il quarto segnale apre una botola di coriandoli sotto {subject}"
        progression = [
            f"Il segnale uno attiva {first}",
            f"Il segnale due attiva {second}",
            f"Il segnale tre attiva {third}",
            "Il quarto segnale sembra vuoto",
            "Una botola di coriandoli apre il ballo finale",
        ]
        lyrics = _duration_sections(
            ["Quattro cerchi aspettano sul pavimento,", "Emma li accende seguendo il tempo."],
            [
                f"Uno fa {first}, due fa {second},",
                f"tre chiama {third} e fa vibrare il mondo.",
                f"Quattro sembra vuoto sotto {subject},",
                "poi una botola apre coriandoli blu!",
            ],
            [
                f"Uno {first}, due {second},",
                f"tre {third}, quattro salta in fondo!",
                "Ogni numero accende una parte del ballo:",
                "segui Emma e atterra dentro il cerchio giallo!",
            ],
            [
                "I cerchi cambiano posto ma il conto resta uguale,",
                f"{subject} li attraversa con un passo laterale.",
                "Emma tiene il ritmo con quattro colpi netti,",
                "il cast risponde saltando sui dischetti.",
            ],
            [
                f"Uno {first}, due {second},",
                f"tre {third}, quattro gira in fondo!",
                f"Emma e {subject} aprono il gran finale:",
                "quattro mosse accendono il nostro carnevale!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    return concept, gag, progression, lyrics


def _peekaboo_song(
    episode: Episode,
    archetype: str,
) -> tuple[str, str, list[str], str]:
    targets = _targets(episode, ["stella", "luna", "coniglietto"])
    first = targets[0]
    second = targets[1] if len(targets) > 1 else "luna"
    subject = _subject(episode, first)
    hiding_place = second if _plain(second) != _plain(subject) else "nuvola"
    concepts = {
        "indovinello": f"Emma riconosce {subject} da tre piccoli indizi",
        "domanda_e_risposta": f"Emma chiama {subject}, che risponde da nascondigli diversi",
        "conta_e_scopri": "Ogni conto alla rovescia apre un nascondiglio nuovo",
        "mini_storia_con_finale": f"{subject} scompare ed Emma segue una traccia per ritrovarlo",
        "personaggio_pasticcione": f"{subject} sceglie nascondigli sempre troppo piccoli",
    }
    concept = concepts[archetype]
    if archetype == "indovinello":
        gag = f"{subject} nasconde tutto tranne due piedini"
        progression = [
            f"{subject} scompare davanti a Emma",
            "Restano visibili soltanto due piedini",
            "Un piccolo suono offre il secondo indizio",
            "Emma unisce gli indizi e indovina",
            f"{subject} compare dal nascondiglio finale",
        ]
        lyrics = _duration_sections(
            [f"Pop! {subject} non si vede più,", "restano due piedini che tremano laggiù."],
            [
                f"Emma cerca dietro {hiding_place}: niente faccia,",
                "ma una piccola risata lascia la traccia.",
                "Un fruscio leggero, poi toc toc toc:",
                f"salta fuori {second}… ma {subject} ancora no!",
            ],
            [
                "Due piedini e una risata:",
                "chi si nasconde dietro la facciata?",
                f"Emma unisce gli indizi: «Sei {subject}!»",
                "Apri il nascondiglio: cucù, proprio tu!",
            ],
            [
                "Spunta una manina, poi torna al riparo,",
                "Emma riconosce quel gesto così raro.",
                "La tenda si gonfia, il mistero è finito,",
                f"{subject} salta fuori con un piccolo grido.",
            ],
            [
                f"Erano i piedini di {subject}!",
                "La risata aveva spiegato già.",
                "Emma chiude gli occhi, ora indovini tu:",
                "un altro piccolo indizio… e grande cucù!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    elif archetype == "domanda_e_risposta":
        gag = f"{subject} risponde ogni volta da una direzione diversa"
        progression = [
            f"Emma chiama {subject} davanti alla tenda",
            "Una risposta arriva da sinistra",
            "La seconda risposta arriva dall’alto",
            "Emma segue la terza risposta dietro la nuvola",
            f"{subject} risponde da vicino e compare",
        ]
        lyrics = _duration_sections(
            [f"«{subject}, dove sei?» chiama Emma,", "una voce risponde dal fondo della scena."],
            [
                "«Sei dietro la tenda?» — «No, sono di qua!»",
                "Emma gira a sinistra, la voce è più in là.",
                "«Sei sopra la luna?» — una risata dice no,",
                f"{subject} cambia posto appena Emma si voltò.",
            ],
            [
                f"Emma chiama: «{subject}, rispondi un po’!»",
                "Da vicino: «Cucù!» Da lontano: «Oh-oh!»",
                "Segui la voce, aspetta e poi…",
                f"{subject} compare proprio accanto a noi!",
            ],
            [
                f"Una voce sale dietro {hiding_place},",
                "Emma la segue camminando piano.",
                "La risposta ora arriva proprio dal cappello,",
                f"{subject} ne esce fuori con un grande saltello.",
            ],
            [
                f"Emma chiama: «{subject}, rispondi un po’!»",
                "La voce dice «Cucù!» e non scappa però.",
                f"Emma trova {subject} vicino al suo nasino:",
                "domanda e risposta chiudono il giochino!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    elif archetype == "conta_e_scopri":
        gag = "Al secondo conto si apre il nascondiglio sbagliato"
        progression = [
            "Emma avvia il primo conto alla rovescia",
            f"Il primo nascondiglio rivela {second}",
            "Il secondo conto apre un contenitore vuoto",
            f"Il terzo conto fa comparire {subject}",
            "Il cast ripete il conto e si nasconde insieme",
        ]
        lyrics = _duration_sections(
            ["Tre porticine aspettano Emma in fila,", "dietro una soltanto una risata scintilla."],
            [
                f"Tre, due, uno: si apre {hiding_place},",
                f"esce {second} con un fazzoletto in mano.",
                "Tre, due, uno: la seconda è vuota,",
                "ma una piccola ombra dietro Emma ruota.",
            ],
            [
                "Tre… due… uno… resta fermo tu,",
                "una porta si apre e fa cucù.",
                f"Al terzo giro compare {subject}:",
                "conto, attesa, risata… eccolo su!",
            ],
            [
                f"{subject} sceglie la porta numero tre,",
                "Emma conta senza sbirciare dov’è.",
                "L’ultimo numero cade con un plin,",
                "la porta si spalanca con un salto da lì.",
            ],
            [
                "Tre… due… uno… restiamo quaggiù,",
                "tutte le porte si aprono: cucù!",
                f"Emma e {subject} cambiano posto in fretta:",
                "un nuovo conto ricomincia l’attesa!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    elif archetype == "mini_storia_con_finale":
        gag = f"{subject} lascia una fila di oggetti troppo evidente"
        progression = [
            f"{subject} scompare portando con sé {first}",
            "Emma trova il primo oggetto della traccia",
            f"La traccia gira attorno a {hiding_place}",
            "Un falso nascondiglio rallenta la ricerca",
            f"Emma ritrova {subject} e restituisce {first}",
        ]
        lyrics = _duration_sections(
            [f"{subject} prende {first} e sparisce in un lampo,", "Emma trova una traccia che attraversa il campo."],
            [
                f"Un pezzetto di {first} riposa sul tappeto,",
                "un altro fa una curva dietro un vaso quieto.",
                f"La pista gira attorno a {hiding_place},",
                "Emma segue ogni segno tenendolo per mano.",
            ],
            [
                f"Una traccia porta a {subject},",
                "passa sotto, gira e sale su.",
                f"Emma cerca {first} e trova di più:",
                f"{subject} salta fuori gridando cucù!",
            ],
            [
                "Dietro la scatola c’è soltanto un fiocco,",
                "Emma non si ferma e riparte di colpo.",
                "L’ultima traccia entra nel grande cuscino,",
                f"{subject} aspetta lì con {first} vicino.",
            ],
            [
                f"La pista ha portato da {subject},",
                f"Emma riprende {first} e lo alza su.",
                "Ora lasciano una traccia per gli amici:",
                "un altro cucù con finali felici!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    else:
        gag = f"{subject} sceglie nascondigli piccoli e lascia fuori naso e piedi"
        progression = [
            f"{subject} prova a entrare in un vaso troppo piccolo",
            "Emma vede il naso e finge di non accorgersene",
            "Il secondo nascondiglio lascia fuori i piedi",
            f"{subject} trova un nascondiglio adatto dietro {hiding_place}",
            "Emma lo scopre soltanto dopo la pausa",
        ]
        lyrics = _duration_sections(
            [f"{subject} sceglie un vaso piccolino,", "entra la testa, resta fuori il piedino."],
            [
                "Emma passa accanto e finge di cercare,",
                "il vaso starnutisce e comincia a saltellare.",
                f"{subject} prova allora dietro un cappello,",
                "ma spuntano i piedi e anche un codino bello.",
            ],
            [
                f"{subject}, il nascondiglio non va!",
                "Naso fuori, piedi in libertà.",
                f"Trova uno spazio dietro {hiding_place}:",
                "aspetta il silenzio e sorprendi Emma piano!",
            ],
            [
                f"Dietro {hiding_place} finalmente c’è posto,",
                f"{subject} resta fermo e non viene più esposto.",
                "Emma guarda altrove, poi sente un fruscio,",
                "si volta lentamente e scopre l’amico suo.",
            ],
            [
                f"{subject}, quel nascondiglio ora va!",
                "Niente naso o piedi in libertà.",
                "Emma fa una pausa, poi salta lassù:",
                "pasticcio risolto e grandissimo cucù!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    return concept, gag, progression, lyrics


def _story_song(
    episode: Episode,
    archetype: str,
) -> tuple[str, str, list[str], str]:
    subject = _subject(episode, "Nuvi")
    targets = _targets(episode, ["campanella", "ponte", "festa"])
    goal = targets[-1]
    concepts = {
        "mini_storia_con_finale": f"{subject} deve raggiungere {goal} superando due piccoli ostacoli",
        "errore_e_correzione": f"Due soluzioni sbagliate aiutano Emma a trovare quella giusta",
        "inseguimento_musicale": f"Una traccia sonora guida Emma e {subject} fino a {goal}",
        "personaggio_pasticcione": f"{subject} complica il problema ogni volta che prova ad aiutare",
        "canzone_cumulativa": "Ogni tentativo lascia un oggetto utile per la soluzione finale",
    }
    concept = concepts[archetype]
    if archetype == "mini_storia_con_finale":
        gag = f"{subject} spinge dalla parte sbagliata e torna al punto di partenza"
        progression = [
            f"Emma e {subject} scoprono la strada chiusa",
            "Il primo tentativo fa girare la porta al contrario",
            "Una freccia nascosta offre il primo indizio",
            "Emma combina corda e tamburo",
            f"La strada si apre e il cast raggiunge {goal}",
        ]
        lyrics = _duration_sections(
            [f"Din! La strada verso {goal} si è chiusa,", "Emma e gli amici cercano una via."],
            [
                f"{subject} spinge forte: la porta gira,",
                "fa un giro completo e davanti li ritira.",
                "Emma nota una freccia sotto un sassolino,",
                "il primo errore mostra già il cammino.",
            ],
            [
                "Prova, cambia, prova un’altra via,",
                "ogni tentativo porta un’idea.",
                f"Emma e {subject}, fianco a fianco,",
                "trovano la strada dopo il primo inciampo.",
            ],
            [
                "Una corda da sola non arriva al gancio,",
                "un tamburo la lancia con un piccolo balzo.",
                "Emma unisce gli indizi trovati per terra,",
                "la porta si apre e la musica si libera.",
            ],
            [
                "Prova, cambia: la strada ora c’è,",
                "ogni piccolo errore ha spiegato il perché.",
                f"Emma e {subject} raggiungono {goal},",
                "il finale trasforma il problema in canzone!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    elif archetype == "errore_e_correzione":
        gag = f"{subject} usa {targets[0]} al contrario e fa volare il cappello di Emma"
        progression = [
            f"Il gruppo deve usare {targets[0]} per raggiungere {goal}",
            f"{subject} prova dal lato sbagliato",
            "Emma osserva l’effetto e cambia posizione",
            "Il secondo tentativo quasi riesce",
            "La correzione finale risolve il problema",
        ]
        lyrics = _duration_sections(
            [f"{targets[0]} blocca il sentiero per {goal},", "Emma lo solleva, ma ricade da sé."],
            [
                f"{subject} lo gira dalla parte sbagliata,",
                "una folata porta via il cappello sulla strada.",
                "Emma osserva dove punta la freccia blu,",
                f"sposta {targets[0]} e lo appoggia più su.",
            ],
            [
                "Sbaglia, osserva, correggi un po’,",
                "cambia un gesto e riprova però.",
                f"Emma e {subject} capiscono il perché:",
                "la soluzione nasce dall’errore che c’è.",
            ],
            [
                "Il secondo tentativo arriva quasi al gancio,",
                f"{subject} fa più piano e controlla il bilancio.",
                "Emma inclina il pezzo soltanto di un dito,",
                "la strada si apre: il problema è finito.",
            ],
            [
                "Sbaglia, osserva: ora il gesto si può,",
                "cambia un dettaglio e funziona però.",
                f"Emma e {subject} corrono verso {goal}:",
                "la correzione diventa il finale per te!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    elif archetype == "inseguimento_musicale":
        gag = "La traccia sonora passa dentro un tubo e torna alle spalle"
        progression = [
            "Una campanella lontana offre il primo indizio",
            f"Emma e {subject} seguono il ritmo",
            "Il suono attraversa un tubo e cambia direzione",
            "La melodia diventa più chiara vicino alla meta",
            f"Il gruppo raggiunge {goal} seguendo l’ultima nota",
        ]
        lyrics = _duration_sections(
            [f"Din din dan, una nota corre verso {goal},", f"Emma e {subject} la seguono perché."],
            [
                "La nota passa sotto il ponte e sale sul tetto,",
                "lascia tre battiti vicino a un rametto.",
                "Entra in un tubo e sembra andare più in là,",
                "poi suona alle spalle: din din dan!",
            ],
            [
                "Segui la nota: din din dan,",
                "gira col ritmo, tam tam tam.",
                f"Emma e {subject} non perdono il suono:",
                "ogni battito indica il posto più buono.",
            ],
            [
                "La melodia ora brilla dietro una porta,",
                "un passo più forte e la distanza è più corta.",
                f"Vicino a {goal} diventa un piccolo coro,",
                "Emma trova la chiave seguendo il suono d’oro.",
            ],
            [
                "Ecco la nota: din din dan,",
                f"ci ha portati fino a {goal}, tam tam tam.",
                f"Emma e {subject} la cantano insieme:",
                "l’inseguimento musicale ha trovato ciò che preme!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    elif archetype == "personaggio_pasticcione":
        gag = f"{subject} aggiusta un pezzo e ne fa cadere due"
        progression = [
            f"Emma trova il passaggio per {goal} rotto",
            f"{subject} prova a ripararlo troppo in fretta",
            "La riparazione fa cadere altri due pezzi",
            "Emma ordina i pezzi per forma e dimensione",
            "Il personaggio segue l’ordine e completa il passaggio",
        ]
        lyrics = _duration_sections(
            [f"Il ponticello per {goal} fa crac,", f"{subject} corre con colla, corda e tac."],
            [
                "Mette un pezzo tondo dentro il posto quadrato,",
                "ne sistema uno e due cadono di lato.",
                "Emma raccoglie tutto e prepara una fila:",
                "grande, medio, piccolo, la forma scintilla.",
            ],
            [
                f"Piano, {subject}, un pezzo per volta,",
                "trova la forma e poi guarda la svolta.",
                "Emma mette ordine, tu segui la via:",
                "anche un pasticcione risolve in compagnia!",
            ],
            [
                f"{subject} prende il grande e lo posa laggiù,",
                "il medio entra al centro, il piccolo più su.",
                "Questa volta nessun pezzo cade giù,",
                "il ponte resta fermo e non traballa più.",
            ],
            [
                f"Bravo, {subject}, un pezzo per volta,",
                f"il ponte verso {goal} apre la svolta.",
                "Emma passa per prima e tende la manina:",
                "il pasticcio è diventato una strada carina!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    else:
        gag = f"{subject} porta tutti gli oggetti insieme e non vede più la strada"
        progression = [
            f"Emma trova {targets[0]}",
            f"Il secondo tentativo aggiunge {targets[1] if len(targets) > 1 else 'una corda'}",
            "Ogni oggetto risolve una parte del percorso",
            f"{subject} prova a portarli tutti e inciampa",
            "Emma usa gli oggetti nella sequenza corretta",
        ]
        second_item = targets[1] if len(targets) > 1 else "corda"
        lyrics = _duration_sections(
            [f"Emma trova {targets[0]} vicino al sentiero,", f"per arrivare a {goal} servirà davvero."],
            [
                f"Prima {targets[0]} solleva il cancello,",
                f"poi {second_item} attraversa il ruscello.",
                f"{subject} porta tutto davanti agli occhi,",
                "non vede un sasso e fa cadere i blocchi.",
            ],
            [
                f"Prima {targets[0]}, poi {second_item},",
                "ogni oggetto risolve un pezzetto.",
                f"Emma li usa nell’ordine giusto:",
                f"passo dopo passo si arriva a {goal}.",
            ],
            [
                f"{targets[0]} resta al cancello, non va portato,",
                f"{second_item} resta sul ponte ben annodato.",
                f"{subject} ora cammina con le mani leggere,",
                "la strada completa comincia a vedere.",
            ],
            [
                f"Prima {targets[0]}, poi {second_item},",
                "ogni oggetto è rimasto al suo posto.",
                f"Emma e {subject} raggiungono {goal}:",
                "la sequenza ha costruito il finale che c’è!",
            ],
            duration_seconds=episode.duration_seconds,
        )
    return concept, gag, progression, lyrics


def _bedtime_song(
    episode: Episode,
    archetype: str,
) -> tuple[str, str, list[str], str]:
    subject = _subject(episode, "stellina")
    targets = _targets(episode, ["luna", "nuvola", "stella"])
    first = targets[0]
    second = targets[1] if len(targets) > 1 else "nuvola"
    concepts = {
        "canzone_cumulativa": "Ogni luce si spegne e aggiunge una carezza al rituale",
        "mini_storia_con_finale": f"Emma accompagna {subject} fino al suo letto di nuvole",
        "domanda_e_risposta": f"Emma sussurra e {subject} risponde con una luce sempre più tenue",
        "conta_e_scopri": "Quattro respiri fanno addormentare il cielo un pezzo alla volta",
        "trasformazione_progressiva": "Il cielo passa lentamente dal tramonto alla notte quieta",
    }
    concept = concepts[archetype]
    if archetype == "canzone_cumulativa":
        gag = f"Una piccola stella sbadiglia e riaccende per sbaglio {first}"
        progression = [
            "Emma abbassa la prima luce",
            "La seconda luce si aggiunge al rituale",
            f"{subject} riaccende per errore una luce",
            "Emma ripete la sequenza con un respiro",
            "Tutte le luci si spengono nello stesso ordine",
        ]
        lyrics = _duration_sections(
            ["Una luce saluta la sera che va,", "Emma la copre con una carezza."],
            [
                "Prima una stella chiude gli occhi già,",
                "poi una seconda le si posa qua.",
                f"{subject} sbadiglia e riaccende {first},",
                "Emma soffia piano e la luce torna in basso.",
            ],
            [
                "Prima una luce, poi un’altra va giù,",
                "ogni respiro ne accompagna una in più.",
                f"Emma culla {subject} sulla scia della luna:",
                "la sequenza della sera prepara la culla.",
            ],
            [
                "Prima la stella, poi il piccolo faro,",
                "dopo la luna lascia un riflesso più chiaro.",
                f"{second} si chiude come un morbido fiore,",
                "ogni luce riposa seguendo il suo cuore.",
            ],
            [
                "Prima una luce, poi l’ultima va giù,",
                "ogni respiro ha accompagnato di più.",
                f"Emma e {subject} dormono sulla luna:",
                "la sequenza è finita dentro una culla.",
            ],
            duration_seconds=episode.duration_seconds,
        )
    elif archetype == "mini_storia_con_finale":
        gag = f"{subject} prova il letto di {first}, ma rimbalza su una nuvola"
        progression = [
            f"{subject} cerca il proprio letto nel cielo",
            f"Il primo posto vicino a {first} è troppo luminoso",
            "Una nuvola elastica lo fa rimbalzare",
            "Emma trova una culla tra due nuvole quiete",
            f"{subject} si addormenta e il cielo abbassa la luce",
        ]
        lyrics = _duration_sections(
            [f"{subject} sbadiglia: il suo letto non c’è,", "Emma lo cerca nel cielo con sé."],
            [
                f"Vicino a {first} la luce è troppo viva,",
                "sopra una nuvola rimbalza e poi scivola.",
                f"Emma apre un varco tra {second} e la luna,",
                "lì trova nascosta una piccola culla.",
            ],
            [
                f"Dormi, {subject}, la strada finisce qua,",
                "Emma ha trovato dove il sogno verrà.",
                "Una nuvola sotto, la luna più su,",
                "il cielo fa piano e riposi anche tu.",
            ],
            [
                f"{subject} si stende, ma manca un cuscino,",
                "Emma piega una nuvola e la porta vicino.",
                "La coperta è una scia che la stella lasciò,",
                "la culla si chiude e il vento rallentò.",
            ],
            [
                f"Dormi, {subject}, il tuo letto ora c’è,",
                "Emma resta accanto e respira con te.",
                "Una nuvola sotto, la luna più su,",
                "la storia si addormenta e riposi anche tu.",
            ],
            duration_seconds=episode.duration_seconds,
        )
    elif archetype == "domanda_e_risposta":
        gag = f"{subject} risponde con uno sbadiglio che fa tremare {first}"
        progression = [
            f"Emma chiede a {subject} se è pronto",
            "Il personaggio risponde con una luce forte",
            "Emma abbassa la voce e ripete la domanda",
            "La risposta diventa un piccolo sbadiglio",
            "Domanda e risposta si spengono insieme",
        ]
        lyrics = _duration_sections(
            [f"Emma sussurra: «{subject}, sei stanco?»", "Una luce risponde brillando lì accanto."],
            [
                "«Vuoi una nuvola?» — «Morbida, sì.»",
                "«Vuoi la luna?» — «Vicino, così.»",
                f"{subject} sbadiglia e fa tremare {first},",
                "Emma abbassa la voce e lo chiama più piano.",
            ],
            [
                f"«Buonanotte, {subject}.» — «Notte, Emma.»",
                "Una voce si spegne insieme alla scena.",
                "Domanda sottovoce, risposta quaggiù:",
                "il cielo fa piano e riposi anche tu.",
            ],
            [
                "«Chiude gli occhi la stella?» — «Adesso, sì.»",
                "«Resta un respiro?» — «Soltanto così.»",
                f"{second} risponde con un piccolo fruscio,",
                "Emma lascia dormire anche il vento suo.",
            ],
            [
                f"«Buonanotte, {subject}.» — «Notte, Emma.»",
                "L’ultima risposta si posa sulla scena.",
                "Domanda sottovoce, silenzio quaggiù:",
                "il cielo si è spento e riposi anche tu.",
            ],
            duration_seconds=episode.duration_seconds,
        )
    elif archetype == "conta_e_scopri":
        gag = "Al terzo respiro una stellina fa uno sbadiglio sonoro"
        progression = [
            "Il primo respiro abbassa una luce",
            "Il secondo respiro quieta il vento",
            "Il terzo respiro provoca uno sbadiglio",
            "Il quarto respiro chiude la luna",
            "Emma ripete il conteggio in silenzio",
        ]
        lyrics = _duration_sections(
            ["Quattro respiri aspettano la sera,", "Emma apre le mani, la notte si avvicina."],
            [
                "Uno: una luce scende un po’,",
                "due: anche il vento rallentò.",
                "Tre: una stellina sbadiglia «ah-ah»,",
                "quattro: la luna gli occhi chiuderà.",
            ],
            [
                "Uno entra piano, due se ne va,",
                "tre porta quiete, quattro resterà.",
                f"Emma conta accanto a {subject}:",
                "quattro respiri e il cielo dorme già.",
            ],
            [
                "Emma ricomincia senza alzare la voce,",
                f"{first} perde piano l’ultima luce.",
                f"{second} si ferma sopra il tetto blu,",
                f"al quarto respiro dorme anche {subject}.",
            ],
            [
                "Uno entra piano, due se ne va,",
                "tre porta quiete, quattro resterà.",
                f"Emma chiude gli occhi accanto a {subject}:",
                "il conto è finito, il cielo dorme già.",
            ],
            duration_seconds=episode.duration_seconds,
        )
    else:
        gag = f"Una stella resta accesa sul naso di {subject}"
        progression = [
            "Il tramonto colora il cielo di pesca",
            f"La luce su {first} diventa viola",
            f"{second} si trasforma in un cuscino blu",
            f"Una stella resta accesa sul naso di {subject}",
            "Emma soffia e completa la notte quieta",
        ]
        lyrics = _duration_sections(
            ["Il cielo color pesca saluta il giorno,", "Emma vede la sera cambiare tutt’intorno."],
            [
                f"{first} diventa viola e poi blu,",
                f"{second} si piega e scende più giù.",
                f"Una stella rimane sul naso di {subject},",
                "Emma soffia piano e la porta lassù.",
            ],
            [
                "Pesca, viola, blu della sera,",
                "ogni colore si quieta e si altera.",
                f"Emma accompagna {subject} dentro la luna:",
                "il cielo trasformato diventa una culla.",
            ],
            [
                "Le ultime strisce del giorno vanno via,",
                "la notte distende la sua melodia.",
                f"{second} cambia forma e copre il sentiero,",
                "il mondo diventa più lento e leggero.",
            ],
            [
                "Viola, blu, notte davvero,",
                "ogni colore riposa nel cielo.",
                f"Emma e {subject} dormono sulla luna:",
                "la trasformazione è diventata una culla.",
            ],
            duration_seconds=episode.duration_seconds,
        )
    return concept, gag, progression, lyrics


def _english_song(
    episode: Episode,
    archetype: str,
    song_format: str,
) -> tuple[str, str, list[str], str]:
    subject = _subject(episode, "little star")
    targets = _targets(episode, ["red", "blue", "star"])
    first = targets[0]
    second = targets[1] if len(targets) > 1 else "blue"
    concept = f"{subject} changes the scene through a {ARCHETYPE_LABELS[archetype].lower()} pattern"
    gag = f"{subject} chooses {second} when Emma signals {first}"
    progression = [
        f"Emma discovers the problem around {subject}",
        f"The first action reveals {first}",
        "A comic mistake changes the plan",
        f"The corrected action reveals {second}",
        "The cast repeats the specific solution in the finale",
    ]
    lyrics = _duration_sections(
        [f"Pop! {subject} jumps into view,", f"Emma spots a trail of {first} and {second}."],
        [
            f"{subject} takes one step, then misses the mark,",
            "Emma finds a clue that glows in the dark.",
            f"Touch the {first}, let the little bell ring,",
            "one clear action changes everything.",
        ],
        [
            f"{subject}, follow the {first} trail,",
            f"turn at {second} and lift the sail.",
            "One small move, one bright reply:",
            "that is how our new game flies!",
        ],
        [
            f"The second clue rolls under {subject}'s feet,",
            "Emma changes the rhythm and follows the beat.",
            f"{first} meets {second} in one clear view,",
            "the whole scene changes into something new.",
        ],
        [
            f"{subject}, follow the {first} trail,",
            f"turn at {second}; we found the tale.",
            "Emma shows the move that made it right,",
            "the cast repeats it in the final light!",
        ],
        duration_seconds=episode.duration_seconds,
    )
    return concept, gag, progression, lyrics


def _build_candidate(
    episode: Episode,
    song_format: str,
    archetype: str,
) -> SongCandidate:
    if episode.language == "en":
        concept, gag, progression, lyrics = _english_song(
            episode,
            archetype,
            song_format,
        )
    elif song_format == "animali_e_versi":
        concept, gag, progression, lyrics = _animal_song(episode, archetype)
    elif song_format == "colori_e_trasformazioni":
        concept, gag, progression, lyrics = _color_song(episode, archetype)
    elif song_format == "baby_dance":
        concept, gag, progression, lyrics = _dance_song(episode, archetype)
    elif song_format == "cucu_e_sorpresa":
        concept, gag, progression, lyrics = _peekaboo_song(episode, archetype)
    elif song_format == "nanna":
        concept, gag, progression, lyrics = _bedtime_song(episode, archetype)
    else:
        concept, gag, progression, lyrics = _story_song(episode, archetype)
    return SongCandidate(
        archetype=archetype,
        concept=concept,
        gag=gag,
        progression=progression,
        lyrics=lyrics.strip(),
    )


def count_syllables(line: str) -> int:
    words = WORD_RE.findall(_plain(line))
    total = 0
    for word in words:
        groups = re.findall(r"[aeiouy]+", word)
        total += max(1, len(groups))
    return total


def lyric_similarity(left: str, right: str) -> float:
    left_normalized = _normalized_text(left)
    right_normalized = _normalized_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    left_words = left_normalized.split()
    right_words = right_normalized.split()
    left_shingles = {
        tuple(left_words[index : index + 3])
        for index in range(max(1, len(left_words) - 2))
    }
    right_shingles = {
        tuple(right_words[index : index + 3])
        for index in range(max(1, len(right_words) - 2))
    }
    union = left_shingles | right_shingles
    jaccard = len(left_shingles & right_shingles) / len(union) if union else 0.0
    sequence = SequenceMatcher(
        None,
        left_normalized,
        right_normalized,
        autojunk=False,
    ).ratio()
    left_lines = set(_normalized_lines(left))
    right_lines = set(_normalized_lines(right))
    line_union = left_lines | right_lines
    line_overlap = len(left_lines & right_lines) / len(line_union) if line_union else 0.0
    return round(max(jaccard, line_overlap, sequence * 0.75), 4)


def reused_recent_phrases(lyrics: str, memory: CatalogMemory) -> list[str]:
    candidate_lines = {
        line for line in _normalized_lines(lyrics) if len(line.split()) >= 3
    }
    recent_lines = {
        line
        for recent in memory.recent_lyrics
        for line in _normalized_lines(recent)
        if len(line.split()) >= 3
    }
    return sorted(candidate_lines & recent_lines)


def _archetype_fit(
    episode: Episode,
    song_format: str,
    archetype: str,
) -> int:
    brief = _plain(
        " ".join(
            [
                episode.title,
                episode.hook,
                *episode.target_words,
                *episode.featured_characters,
            ]
        )
    )
    score = 80
    if song_format == "animali_e_versi" and archetype == "errore_e_correzione":
        if any(token in brief for token in ("pappi", "pappagall", "sbagli", "confond")):
            score += 20
    if "pasticc" in brief and archetype == "personaggio_pasticcione":
        score += 18
    if "trasform" in brief and archetype == "trasformazione_progressiva":
        score += 14
    if "cerca" in brief and archetype in {
        "inseguimento_musicale",
        "indovinello",
    }:
        score += 12
    if "cont" in brief and archetype == "conta_e_scopri":
        score += 12
    return min(100, score)


def _specific_refrain(episode: Episode, lyrics: str) -> bool:
    refrains = _refrains(lyrics)
    if not refrains:
        return False
    refrain = _normalized_text(refrains[0])
    anchors = [
        _normalized_text(value)
        for value in [
            *_supporting_characters(episode),
            *episode.target_words,
        ]
        if _normalized_text(str(value))
    ]
    return any(anchor in refrain for anchor in anchors)


def _score_candidate(
    candidate: SongCandidate,
    episode: Episode,
    song_format: str,
    memory: CatalogMemory,
) -> None:
    similarities = [
        lyric_similarity(candidate.lyrics, previous)
        for previous in memory.recent_lyrics
    ]
    max_similarity = max(similarities, default=0.0)
    blocked_hits = [
        phrase
        for phrase in memory.blocked_phrases
        if _normalized_text(phrase) in _normalized_text(candidate.lyrics)
    ]
    reused_lines = reused_recent_phrases(candidate.lyrics, memory)
    rhyme_overlap = len(
        set(_rhyme_pairs(candidate.lyrics))
        & set(memory.recent_rhyme_pairs)
    )
    recent_archetype_count = memory.recent_archetypes.count(candidate.archetype)
    recent_gag = _normalized_text(candidate.gag) in {
        _normalized_text(gag) for gag in memory.recent_gags
    }
    recent_actions = {
        _normalized_text(action)
        for sequence in memory.recent_action_sequences
        for action in sequence
        if _normalized_text(action)
    }
    reused_actions = sorted(
        {
            _normalized_text(action)
            for action in candidate.progression
            if _normalized_text(action) in recent_actions
        }
    )
    candidate.originality = max(
        0,
        round(
            100
            - max_similarity * 70
            - len(blocked_hits) * 22
            - max(0, len(reused_lines) - 1) * 15
            - min(15, rhyme_overlap * 3)
            - min(18, recent_archetype_count * 6)
            - (15 if recent_gag else 0)
            - max(0, len(reused_actions) - 1) * 12
        ),
    )

    lines = _normalized_lines(candidate.lyrics)
    word_counts = [len(line.split()) for line in lines]
    syllables = [
        count_syllables(line)
        for line in candidate.lyrics.splitlines()
        if line.strip() and not SECTION_RE.match(line.strip())
    ]
    long_lines = sum(count > 11 for count in word_counts)
    extreme_meter = sum(value < 4 or value > 18 for value in syllables)
    meter_span = max(syllables, default=0) - min(syllables, default=0)
    candidate.singability = max(
        0,
        100
        - long_lines * 15
        - extreme_meter * 8
        - max(0, meter_span - 9) * 2,
    )
    if not _refrains(candidate.lyrics):
        candidate.singability -= 25

    fit = _archetype_fit(episode, song_format, candidate.archetype)
    if song_format == "nanna":
        bpm_fit = 100 if 60 <= episode.bpm <= 82 else max(35, 100 - abs(72 - episode.bpm) * 3)
    else:
        bpm_fit = 100 if 80 <= episode.bpm <= 125 else 70
    candidate.energy = round((fit * 0.7) + (bpm_fit * 0.3))

    normalized = _normalized_text(candidate.lyrics)
    target_hits = sum(
        _normalized_text(word) in normalized
        for word in episode.target_words[:5]
        if _normalized_text(word)
    )
    progression_score = 100 if len(candidate.progression) >= 5 else 70
    specificity = 100 if _specific_refrain(episode, candidate.lyrics) else 45
    target_score = 100 if not episode.target_words else min(100, 55 + target_hits * 15)
    candidate.coherence = round(
        progression_score * 0.35
        + specificity * 0.4
        + target_score * 0.25
    )

    candidate.final_score = round(
        candidate.originality * 0.38
        + candidate.singability * 0.24
        + candidate.energy * 0.14
        + candidate.coherence * 0.24
    )
    if blocked_hits:
        candidate.rejection_reasons.append(
            "Contiene formule bloccate: " + ", ".join(blocked_hits)
        )
    if len(reused_lines) > 1:
        candidate.rejection_reasons.append(
            "Riutilizza più di una frase dagli ultimi 10 episodi"
        )
    if rhyme_overlap > 1:
        candidate.rejection_reasons.append(
            "Riutilizza più di una coppia di rime dagli ultimi 10 episodi"
        )
    if len(reused_actions) > 1:
        candidate.rejection_reasons.append(
            "Riutilizza una sequenza di azioni degli ultimi 10 episodi"
        )
    if max_similarity >= 0.48:
        candidate.rejection_reasons.append(
            f"Similarità catalogo troppo alta ({round(max_similarity * 100)}%)"
        )
    if candidate.originality < 62:
        candidate.rejection_reasons.append(
            f"Originalità insufficiente ({candidate.originality}/100)"
        )
    if not _specific_refrain(episode, candidate.lyrics):
        candidate.rejection_reasons.append(
            "Ritornello non abbastanza specifico per tema o personaggio"
        )
    candidate.rejected = bool(candidate.rejection_reasons)


def _candidate_archetypes(
    episode: Episode,
    song_format: str,
    memory: CatalogMemory,
) -> list[str]:
    compatible = list(FORMAT_ARCHETYPES[song_format])
    seed_material = "|".join(
        [
            episode.title,
            episode.hook,
            ",".join(episode.target_words),
            ",".join(episode.featured_characters),
        ]
    )
    offset = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:8], 16)
    offset %= len(compatible)
    rotated = compatible[offset:] + compatible[:offset]
    return sorted(
        rotated,
        key=lambda archetype: (
            -_archetype_fit(episode, song_format, archetype),
            memory.recent_archetypes.count(archetype),
            rotated.index(archetype),
        ),
    )[:4]


def generate_song(
    episode: Episode,
    *,
    recent_episodes: Iterable[Episode] = (),
    catalog_episodes: Iterable[Episode] | None = None,
) -> SongGeneration:
    recent = list(recent_episodes)
    catalog = list(catalog_episodes) if catalog_episodes is not None else recent
    memory = build_catalog_memory(recent, catalog)
    song_format = resolve_song_format(episode)
    candidates = [
        _build_candidate(episode, song_format, archetype)
        for archetype in _candidate_archetypes(episode, song_format, memory)
    ]
    for candidate in candidates:
        _score_candidate(candidate, episode, song_format, memory)
    eligible = [candidate for candidate in candidates if not candidate.rejected]
    if not eligible:
        raise RuntimeError(
            "Nessuno dei quattro concept supera il controllo di originalità "
            "rispetto agli ultimi 10 episodi. Modifica hook, personaggi o "
            "parole target prima di rigenerare."
        )
    selected = max(
        eligible,
        key=lambda candidate: (
            candidate.final_score,
            candidate.originality,
            candidate.coherence,
        ),
    )
    return SongGeneration(
        lyrics=selected.lyrics,
        song_format=song_format,
        selected=selected,
        candidates=candidates,
        memory=memory,
    )


def editorial_audit(
    episode: Episode,
    *,
    recent_episodes: Iterable[Episode] = (),
    catalog_episodes: Iterable[Episode] | None = None,
) -> dict:
    recent = list(recent_episodes)
    catalog = list(catalog_episodes) if catalog_episodes is not None else recent
    memory = build_catalog_memory(recent, catalog)
    lyrics = episode.lyrics_text or ""
    similarities = [
        lyric_similarity(lyrics, previous)
        for previous in memory.recent_lyrics
    ]
    max_similarity = max(similarities, default=0.0)
    reused_lines = reused_recent_phrases(lyrics, memory)
    normalized = _normalized_text(lyrics)
    blocked_hits = [
        phrase
        for phrase in memory.blocked_phrases
        if _normalized_text(phrase) in normalized
    ]
    lines = [
        line.strip()
        for line in lyrics.splitlines()
        if line.strip() and not SECTION_RE.match(line.strip())
    ]
    word_counts = [len(WORD_RE.findall(line)) for line in lines]
    syllables = [count_syllables(line) for line in lines]
    verbs = _verbs(lyrics)
    unique_verbs = set(verbs)
    sections = _sections(lyrics)
    verse_sections = [
        section_lines
        for name, section_lines in sections
        if "strofa" in _plain(name) or "verse" in _plain(name)
    ]
    verse_signatures = [
        set(_normalized_text(" ".join(section_lines)).split())
        for section_lines in verse_sections
    ]
    distinct_verses = True
    if len(verse_signatures) > 1:
        left, right = verse_signatures[0], verse_signatures[1]
        distinct_verses = (
            len(left & right) / max(1, len(left | right))
        ) < 0.6
    current_actions = {
        _normalized_text(str(scene.get("action", "")))
        for scene in (episode.storyboard_json or [])
        if _normalized_text(str(scene.get("action", "")))
    }
    recent_actions = {
        _normalized_text(str(scene.get("action", "")))
        for previous in recent
        for scene in (previous.storyboard_json or [])
        if _normalized_text(str(scene.get("action", "")))
    }
    return {
        "max_similarity": max_similarity,
        "similarity_percent": round(max_similarity * 100),
        "reused_phrases": reused_lines,
        "blocked_phrases": blocked_hits,
        "unique_verbs": sorted(unique_verbs),
        "unique_verb_count": len(unique_verbs),
        "word_counts": word_counts,
        "syllables": syllables,
        "meter_span": max(syllables, default=0) - min(syllables, default=0),
        "has_refrain": bool(_refrains(lyrics)),
        "specific_refrain": _specific_refrain(episode, lyrics),
        "has_progression": len(verse_sections) <= 1 or distinct_verses,
        "structure": list(_structure(lyrics)),
        "song_format": resolve_song_format(episode),
        "rhyme_pair_overlap": len(
            set(_rhyme_pairs(lyrics)) & set(memory.recent_rhyme_pairs)
        ),
        "reused_storyboard_actions": sorted(
            current_actions & recent_actions
        ),
    }

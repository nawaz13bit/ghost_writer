"""Author personas: distinct genre voices that can write solo or collaborate.

Each persona is a named voice profile (not a separate model - all personas run
on the same local LLM, differentiated by system prompt). The pipeline picks
one or more personas based on genre, and when more than one is active they
"collaborate": each persona is described to the model along with a rule for
how to divide or blend the work, so a cross-genre book (e.g. "sci-fi horror")
can draw on both voices in the same chapter.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Persona:
    key: str
    display_name: str
    genres: list[str]  # keywords matched against the book's genre string
    voice: str  # description of prose style, injected into the system prompt


PERSONAS: dict[str, Persona] = {
    "thriller": Persona(
        key="thriller",
        display_name="Vance Cole",
        genres=["thriller", "action", "spy"],
        voice=(
            "Writes lean, propulsive prose: short punchy sentences under pressure, "
            "longer sentences to build dread before a beat, frequent scene-ending hooks, "
            "and a relentless forward sense of time and stakes."
        ),
    ),
    "scifi": Persona(
        key="scifi",
        display_name="Nadia Okoro",
        genres=["sci-fi", "scifi", "science fiction", "space", "cyberpunk", "dystopian"],
        voice=(
            "Writes with precise, idea-driven prose: grounds speculative concepts in concrete "
            "sensory detail and character stakes rather than exposition dumps, favors a sense "
            "of scale and wonder undercut by human-level consequence."
        ),
    ),
    "horror": Persona(
        key="horror",
        display_name="Edgar Marsh",
        genres=["horror", "gothic", "supernatural"],
        voice=(
            "Writes with slow-building dread: precise, unsettling sensory detail, restraint "
            "over gore, ambiguity that lets the reader's imagination do the work, and prose "
            "rhythm that tightens as tension escalates."
        ),
    ),
    "comedy": Persona(
        key="comedy",
        display_name="Priya Malhotra",
        genres=["comedy", "humor", "satire", "romcom"],
        voice=(
            "Writes with sharp comic timing: wit in the narration itself (not just dialogue), "
            "escalating absurdity grounded in character logic, and a light touch that still "
            "lets real emotional stakes land."
        ),
    ),
    "mystery": Persona(
        key="mystery",
        display_name="Rowan Blackwood",
        genres=["mystery", "detective", "whodunit", "cozy mystery"],
        voice=(
            "Writes with controlled, clue-conscious prose: plants details fairly, favors "
            "close observation and deduction on the page, and paces reveals so the reader "
            "can almost-but-not-quite get there first."
        ),
    ),
    "ya": Persona(
        key="ya",
        display_name="Skylar Reyes",
        genres=["ya", "young adult", "coming-of-age", "teen"],
        voice=(
            "Writes in a direct, emotionally immediate voice grounded in a teenage protagonist's "
            "interiority: contemporary but not gimmicky diction, high emotional stakes tied to "
            "identity and belonging, brisk pacing."
        ),
    ),
    "romance": Persona(
        key="romance",
        display_name="Elena Vasquez",
        genres=["romance", "love story"],
        voice=(
            "Writes with close attention to charged interiority and physical awareness between "
            "characters, escalating emotional intimacy scene by scene, and dialogue that carries "
            "subtext-heavy tension."
        ),
    ),
    "fantasy": Persona(
        key="fantasy",
        display_name="Callum Ashgrove",
        genres=["fantasy", "epic fantasy", "sword and sorcery", "fairy tale"],
        voice=(
            "Writes with immersive, texture-rich prose that earns its wonder through specific "
            "detail rather than grand abstraction, clear stakes within invented systems of magic "
            "or politics, and a strong sense of place."
        ),
    ),
    "noir": Persona(
        key="noir",
        display_name="Dashiell Cruz",
        genres=["noir", "crime", "hardboiled", "heist", "police procedural"],
        voice=(
            "Writes terse, cynical first-person-feeling narration even in third person: "
            "world-weary observation, moral ambiguity, urban decay as atmosphere, clipped "
            "dialogue heavy with subtext, and a fatalistic sense that the city always wins."
        ),
    ),
    "historical": Persona(
        key="historical",
        display_name="Beatrice Alcott",
        genres=["historical", "historical fiction", "period drama", "regency", "victorian"],
        voice=(
            "Writes grounded, period-accurate prose: social customs, manners, and material "
            "detail of the era surface through scene and behavior rather than exposition, "
            "diction calibrated to the period without becoming a pastiche, and a clear sense "
            "of the historical stakes bearing down on personal ones."
        ),
    ),
    "literary": Persona(
        key="literary",
        display_name="Marguerite Hale",
        genres=["literary", "drama", "general fiction"],
        voice=(
            "Writes with careful, character-driven prose: interiority over plot mechanics, "
            "precise and often understated language, and attention to theme emerging through "
            "specific detail rather than statement."
        ),
    ),
    "nonfiction": Persona(
        key="nonfiction",
        display_name="Nonfiction voice",
        genres=[],  # never keyword-matched; selected only via book_type="nonfiction"
        voice=(
            "Writes clear, well-organized expository prose: claims are precise and, where a "
            "fact or figure comes from a source, attributed rather than stated as common "
            "knowledge; explains concepts through concrete examples rather than jargon; "
            "maintains a consistent, credible authorial voice without invented dialogue, "
            "invented characters, or fictional scene-setting."
        ),
    ),
}

DEFAULT_PERSONA_KEY = "literary"
NONFICTION_PERSONA_KEY = "nonfiction"


def select_personas_for_genre(genre: str, book_type: str = "fiction") -> list[Persona]:
    """Matches a free-text genre string against persona genre keywords.

    Returns every persona whose keyword appears in the genre string (so
    "sci-fi horror" collaborates scifi + horror), falling back to the
    literary default if nothing matches. When book_type is "nonfiction",
    genre is a subject/category label (e.g. "history") rather than a
    fiction genre, so it's ignored and the nonfiction persona is used.
    """
    if book_type == "nonfiction":
        return [PERSONAS[NONFICTION_PERSONA_KEY]]
    genre_lower = genre.lower()
    matches = [p for p in PERSONAS.values() if any(kw in genre_lower for kw in p.genres)]
    return matches or [PERSONAS[DEFAULT_PERSONA_KEY]]


def get_personas(keys: list[str]) -> list[Persona]:
    missing = [k for k in keys if k not in PERSONAS]
    if missing:
        available = ", ".join(sorted(PERSONAS))
        raise KeyError(f"Unknown persona(s): {missing}. Available: {available}")
    return [PERSONAS[k] for k in keys]


# Named voices for the developmental-editing critique checkers (pacing,
# stakes, craft, book-level rollup). These aren't genre voices like PERSONAS
# above - every book gets the same four editors regardless of genre - so
# they're a separate, much smaller mapping rather than entries in PERSONAS.
CRITIQUE_EDITORS: dict[str, str] = {
    "pacing": "Priya Sato",
    "stakes": "Marcus Webb",
    "craft": "Odette Fontaine",
    "book": "Theo Bracken",
}


def build_collaborative_system_prompt(personas: list[Persona]) -> str:
    if len(personas) == 1:
        p = personas[0]
        if p.key == NONFICTION_PERSONA_KEY:
            return (
                f"You are the author of a nonfiction book. {p.voice}\n"
                "When given sourced research notes to ground the chapter in, cite the specific "
                "facts you draw from them inline using a footnote marker like [^note-id], using "
                "the exact id given for that note - do not invent ids or cite anything not "
                "provided. Write the chapter prose only - no chapter titles, no meta-commentary, "
                "no markdown formatting."
            )
        return (
            f"You are {p.display_name}, a novelist. {p.voice}\n"
            "Write full prose chapters with vivid sensory detail, distinct character voices in "
            "dialogue, and show-don't-tell scene craft. Write the chapter prose only - no chapter "
            "titles, no author notes, no meta-commentary, no markdown formatting."
        )

    roster = "\n".join(f"- {p.display_name}: {p.voice}" for p in personas)
    names = ", ".join(p.display_name for p in personas)
    return (
        f"You are a team of co-authors collaborating on one chapter: {names}.\n"
        f"{roster}\n\n"
        "Blend these voices into a single coherent chapter rather than switching narrators: "
        "let each author's strengths surface where the scene calls for it (e.g. the thriller "
        "voice during action, the horror voice during dread, the comedy voice for banter) while "
        "keeping one consistent narrative voice and tense throughout. Write full prose with vivid "
        "sensory detail, distinct character voices in dialogue, and show-don't-tell scene craft. "
        "Write the chapter prose only - no chapter titles, no author notes, no meta-commentary, "
        "no attribution of lines to a particular author, no markdown formatting."
    )

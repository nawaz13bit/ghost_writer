"""Translator: final-pass literary translation of a finished English manuscript
into another language.

Runs only after a chapter is finalized in English - this is a separate,
post-finalization pass, not part of the drafting/continuity pipeline, so it
never has to reason about bible consistency in a non-English language.

Each target language gets its own TranslatorAgent instance with its own
system_prompt (see LANGUAGES below and their instantiation in webui/state.py),
rather than one agent plus a hardcoded persona dict. That way every language's
cultural/register guidance is an independently editable, resettable prompt in
the existing prompts UI, same as any other agent.
"""
from __future__ import annotations

from dataclasses import dataclass

from ghostwriter.agents.base import Agent, AIOutputError, strip_echoed_delimiters
from ghostwriter.memory.story_bible import StoryBible


@dataclass
class TranslationLanguage:
    key: str  # e.g. "de" - used as the dict key under chapter["translations"]
    name: str  # display name, e.g. "German"
    lang_code: str  # ISO 639-1, used for EPUB dc:language
    pdf_supported: bool  # False for scripts reportlab's base-14 fonts can't render
    cultural_notes: str  # register/idiom/convention guidance for this language


LANGUAGES: list[TranslationLanguage] = [
    TranslationLanguage(
        key="de",
        name="German",
        lang_code="de",
        pdf_supported=True,
        cultural_notes=(
            "Use formal Sie by default for narration-adjacent address and for dialogue between "
            "characters who are not established as close/familiar; switch to du only where the "
            "English clearly signals intimacy, family, or established closeness. Prefer natural "
            "German sentence rhythm and compounding over literal word-for-word order - German "
            "tolerates longer, more embedded clauses than English, so don't chop sentences up just "
            "to match English sentence boundaries. Localize idioms and figures of speech to their "
            "closest natural German equivalent rather than translating them literally; render "
            "measurements/dates/quotation marks („...“) in standard German convention."
        ),
    ),
    TranslationLanguage(
        key="fr",
        name="French",
        lang_code="fr",
        pdf_supported=True,
        cultural_notes=(
            "Use formal vous by default between characters who are not established as close, "
            "switching to tu only where intimacy, family, or long-standing familiarity is clear "
            "from context - getting this wrong is one of the most noticeable errors a native "
            "reader will catch. Prefer elegant, flowing French syntax over literal transposition "
            "of English clause order. Localize idioms to natural French equivalents rather than "
            "calquing them word-for-word. Use French quotation conventions (guillemets « ... » "
            "with narrow no-break spaces) and French punctuation spacing rules."
        ),
    ),
    TranslationLanguage(
        key="es",
        name="Spanish",
        lang_code="es",
        pdf_supported=True,
        cultural_notes=(
            "Use formal usted by default for address between characters who are not established "
            "as close, familiar, or peers, switching to tú where the English signals intimacy or "
            "closeness. Use neutral, internationally readable Spanish rather than country-specific "
            "slang unless the source text itself is regionally marked - avoid idioms that only land "
            "in one dialect (e.g. purely Mexican or purely Argentine slang) unless the character or "
            "setting calls for it. Localize idioms to natural Spanish equivalents rather than "
            "translating them literally. Use Spanish punctuation conventions, including inverted "
            "¿ and ¡ marks."
        ),
    ),
    TranslationLanguage(
        key="ru",
        name="Russian",
        lang_code="ru",
        pdf_supported=False,
        cultural_notes=(
            "Use formal вы (with capitalized Вы in direct address where convention calls for it) "
            "between characters who are not established as close, switching to informal ты only "
            "where the English clearly signals intimacy or closeness. Render names with the "
            "name-plus-patronymic form only where the English original implies that level of formal "
            "address (e.g. a subordinate addressing a superior); otherwise use given name or "
            "surname as the English does, don't invent patronymics. Favor natural Russian sentence "
            "and clause structure over literal English word order, and localize idioms to their "
            "closest natural Russian equivalent rather than calquing them. Use Russian typographic "
            "conventions for dialogue (em dash — to open a line of speech) and quotation marks "
            "(«...»)."
        ),
    ),
    TranslationLanguage(
        key="ja",
        name="Japanese",
        lang_code="ja",
        pdf_supported=False,
        cultural_notes=(
            "Calibrate speech level (keigo/formal desu-masu vs. plain/casual da-form, and honorific "
            "vs. humble forms) per character relationship as it's established in the English text - "
            "a subordinate addressing a superior, strangers, or formal settings take polite/keigo "
            "forms, while close friends or family take plain form; this register choice matters more "
            "to a Japanese reader than literal word choice. Omit pronouns and subjects wherever "
            "natural Japanese would omit them rather than inserting explicit watashi/kare/kanojo "
            "everywhere the English has 'I'/'he'/'she' - dropping the subject is the natural, fluent "
            "form, not a loss of information. Use appropriate honorific suffixes (-san, -sama, -kun, "
            "-chan) consistent with each character's relationship and status, or omit them where the "
            "English implies deliberate informality/rudeness. Localize idioms to natural Japanese "
            "equivalents rather than translating them literally. Use standard Japanese punctuation "
            "and quotation marks (「...」)."
        ),
    ),
    TranslationLanguage(
        key="bn",
        name="Bengali",
        lang_code="bn",
        pdf_supported=False,
        cultural_notes=(
            "Calibrate the pronoun/verb formality register (আপনি apni for formal/respectful "
            "address, তুমি tumi for familiar/peer address, তুই tui for very close intimacy or "
            "addressing children) per character relationship as established in the English text, "
            "rather than defaulting to one register throughout - misjudging this is immediately "
            "obvious to a native reader. Use kinship and honorific terms of address (dada, didi, "
            "kaka, etc.) where the English implies that kind of familial or social relationship "
            "rather than only literal names. Favor natural Bengali sentence rhythm and clause order "
            "over literal transposition of English structure, and localize idioms to their closest "
            "natural Bengali equivalent rather than translating them literally."
        ),
    ),
]

LANGUAGES_BY_KEY: dict[str, TranslationLanguage] = {lang.key: lang for lang in LANGUAGES}


def _system_prompt(lang: TranslationLanguage) -> str:
    return f"""You are an elite literary translator producing the {lang.name} edition of a novel.
You translate with full command of both the source English and target {lang.name}, and you
are deeply versed in {lang.name}-language literary convention and the surrounding culture, not
just the language mechanically - your job is to make sure nothing is lost in translation.

{lang.cultural_notes}

Preserve the plot, characterization, tone, and meaning of the source text exactly - do not add,
omit, or reinterpret content. Produce natural, fluent {lang.name} prose a native reader would
recognize as originally written in {lang.name}, not a word-for-word crib. Output only the
translated chapter text in {lang.name} - no commentary, no notes, no markdown, no English."""


class TranslatorAgent(Agent):
    def __init__(self, llm, language: TranslationLanguage):
        super().__init__(llm)
        self.language = language
        self.system_prompt = _system_prompt(language)

    def translate_glossary(self, names: list[str]) -> dict[str, str]:
        """Translates/transliterates a list of proper names (characters, places,
        factions, etc.) once, so every chapter renders each name the same way -
        without this, a name can drift across a 40-chapter book, which is
        exactly the kind of inconsistency that reads as 'lost in translation'."""
        if not names:
            return {}
        names_block = "\n".join(f"- {name}" for name in names)
        prompt = f"""Here is a list of proper names (character names, place names, faction/organization
names) from an English-language novel that is being translated into {self.language.name}.

{names_block}

For each name, give the exact rendering (transliteration, or an adapted form if {self.language.name}
convention calls for one) that should be used consistently every time this name appears in the
{self.language.name} translation. Keep the same rendering for every occurrence of a name throughout
the book - do not vary it chapter to chapter.

Reply with ONLY a JSON object mapping each original name to its {self.language.name} rendering,
e.g. {{"Original Name": "Rendered Name"}}. Include every name listed above, unchanged as a key."""
        result = self.ask_json_object(prompt, max_tokens=max(self.llm.default_max_tokens, len(names) * 40))
        return {str(k): str(v) for k, v in result.items()}

    def translate(self, bible: StoryBible, chapter_num: int, text: str, glossary: dict[str, str]) -> str:
        glossary_block = ""
        if glossary:
            mapping = "\n".join(f"- {en} -> {tr}" for en, tr in glossary.items())
            glossary_block = f"""

Use these exact renderings for these proper names wherever they appear, for consistency across
the whole book:
{mapping}"""

        prompt = f"""--- CHAPTER {chapter_num} (SOURCE: ENGLISH) ---
{text}
--- END CHAPTER ---
{glossary_block}

Translate this chapter in full into {self.language.name}. Output only the translated chapter
text - no commentary, no English, no markdown."""
        result = self.ask(prompt, max_tokens=max(self.llm.default_max_tokens, int(len(text) * 4)))
        translated = strip_echoed_delimiters(result)
        _guard_translation(text, translated, self.language.name, chapter_num)
        return translated


def _guard_translation(source: str, translated: str, language_name: str, chapter_num: int) -> None:
    """Rejects a translation that's implausibly short compared to the source -
    the local model can truncate on a long chapter (prompt + max_tokens
    exceeding context) or echo back only a fragment, and without this check
    that gets saved as if it were the real, complete translation. Uses
    character counts rather than word counts since word-splitting is
    meaningless for scripts like Japanese that don't use spaces between words."""
    source_chars = len(source.strip())
    translated_chars = len(translated.strip())
    if source_chars >= 200 and translated_chars < source_chars * 0.25:
        raise AIOutputError(
            f"{language_name} translation of chapter {chapter_num} produced {translated_chars} "
            f"characters for a {source_chars}-character source - looks truncated, not saving it"
        )

"""Shared app-wide state: config, LLM client, agent instances, and the
editable-prompt registry. Every router imports from here instead of
constructing its own copies, so there's exactly one instance of each agent
for the life of the server process."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ghostwriter.agents.bible_manager import BibleManagerAgent
from ghostwriter.agents.book_critique import BookCritiqueAgent
from ghostwriter.agents.character_builder import CharacterBuilderAgent
from ghostwriter.agents.continuity_checker import ContinuityCheckerAgent
from ghostwriter.agents.copy_editor import CopyEditorAgent
from ghostwriter.agents.craft_checker import CraftCheckerAgent
from ghostwriter.agents.discuss import DiscussAgent
from ghostwriter.agents.editor import EditorAgent
from ghostwriter.agents.fact_checker import FactCheckerAgent
from ghostwriter.agents.history_compactor import HistoryCompactorAgent
from ghostwriter.agents.pacing_checker import PacingCheckerAgent
from ghostwriter.agents.stakes_checker import StakesCheckerAgent
from ghostwriter.agents.timeline_extractor import TimelineExtractorAgent
from ghostwriter.agents.translator import LANGUAGES, TranslatorAgent
from ghostwriter.agents.outliner import OutlinerAgent
from ghostwriter.agents.project_analyzer import ProjectAnalyzerAgent
from ghostwriter.agents.researcher import ResearcherAgent
from ghostwriter.agents.reviser import ReviserAgent
from ghostwriter.agents.router import RouterAgent
from ghostwriter.agents.voice_checker import VoiceCheckerAgent
from ghostwriter.agents.world_builder import WorldBuilderAgent
from ghostwriter.config import load_config
from ghostwriter.llm_client import LLMClient

STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)

cfg = load_config()
llm = LLMClient(cfg)

editor = EditorAgent(llm)
copy_editor = CopyEditorAgent(llm)
voice_checker = VoiceCheckerAgent(llm)
bible_manager = BibleManagerAgent(llm)
timeline_extractor = TimelineExtractorAgent(llm)
reviser = ReviserAgent(llm)
researcher = ResearcherAgent(llm, **cfg.get("research", {}))
fact_checker = FactCheckerAgent(llm, **cfg.get("research", {}))
world_builder = WorldBuilderAgent(llm)
character_builder = CharacterBuilderAgent(llm)
outliner = OutlinerAgent(llm)
project_analyzer = ProjectAnalyzerAgent(llm)
router_agent = RouterAgent(llm)
discuss_agent = DiscussAgent(llm)
continuity_checker = ContinuityCheckerAgent(llm)
history_compactor = HistoryCompactorAgent(llm)
pacing_checker = PacingCheckerAgent(llm)
stakes_checker = StakesCheckerAgent(llm)
craft_checker = CraftCheckerAgent(llm)
book_critique = BookCritiqueAgent(llm)
translators: dict[str, TranslatorAgent] = {lang.key: TranslatorAgent(llm, lang) for lang in LANGUAGES}

# -- editable agent prompts ---------------------------------------------------
# key -> (human label, agent instance). The writer can override any agent's
# system prompt from the UI; overrides persist in prompt_overrides.json next
# to config.yaml and apply on top of the code defaults at startup.
AGENT_REGISTRY: dict[str, tuple[str, Any]] = {
    "router": ("Prompt router (classifies universal-prompt instructions)", router_agent),
    "discuss": ("Discuss mode (universal prompt bar's brainstorm-first toggle)", discuss_agent),
    "reviser": ("Reviser (applies revise instructions to text)", reviser),
    "continuity_checker": ("Consistency checker (flags affected sections)", continuity_checker),
    "fact_checker": ("Fact checker (flags real-world errors in finalized prose)", fact_checker),
    "editor": ("Editor (finalize pass)", editor),
    "copy_editor": ("Copy editor (finalize pass)", copy_editor),
    "voice_checker": ("Voice checker (finalize pass)", voice_checker),
    "bible_manager": ("Bible sync: character/faction/world (finalize pass)", bible_manager),
    "timeline_extractor": ("Timeline extractor: new plot events (finalize pass)", timeline_extractor),
    "character_builder": ("Character builder (new characters)", character_builder),
    "world_builder": ("World builder (new world entries)", world_builder),
    "researcher": ("Researcher (new notes)", researcher),
    "outliner": ("Outliner (builds/regenerates outline entries)", outliner),
    "project_analyzer": ("Project analyzer (new-project concept analysis)", project_analyzer),
    "history_compactor": ("History compactor (condenses old chapter/entity/scene revisions)", history_compactor),
    "pacing_checker": ("Critique: pacing checker (chapter-level)", pacing_checker),
    "stakes_checker": ("Critique: stakes/tension checker (chapter-level)", stakes_checker),
    "craft_checker": ("Critique: craft checker (show-vs-tell/POV, chapter-level)", craft_checker),
    "book_critique": ("Critique: whole-book rollup", book_critique),
    **{
        f"translator_{key}": (f"Translator: {agent.language.name} (final pass, native cultural persona)", agent)
        for key, agent in translators.items()
    },
}
DEFAULT_PROMPTS = {key: agent.system_prompt for key, (_, agent) in AGENT_REGISTRY.items()}
PROMPT_OVERRIDES_PATH = Path(cfg["paths"]["projects_dir"]).parent / "prompt_overrides.json"


def _load_prompt_overrides() -> dict[str, str]:
    if not PROMPT_OVERRIDES_PATH.exists():
        return {}
    try:
        raw = json.loads(PROMPT_OVERRIDES_PATH.read_text(encoding="utf-8"))
        return {k: v for k, v in raw.items() if k in AGENT_REGISTRY and isinstance(v, str) and v.strip()}
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not read %s - ignoring prompt overrides", PROMPT_OVERRIDES_PATH)
        return {}


def _apply_prompt_overrides(overrides: dict[str, str]) -> None:
    for key, (_, agent) in AGENT_REGISTRY.items():
        agent.system_prompt = overrides.get(key, DEFAULT_PROMPTS[key])


prompt_overrides = _load_prompt_overrides()
_apply_prompt_overrides(prompt_overrides)

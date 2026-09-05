"""Shared book-length presets used by the web UI and the manuscript importer."""
from __future__ import annotations

LENGTH_CATEGORIES = {
    "novella": {"label": "Novella (~30,000-50,000 words)", "chapters": 16, "words_per_chapter": 2200},
    "short_novel": {"label": "Short Novel (~50,000-80,000 words)", "chapters": 24, "words_per_chapter": 2500},
    "novel": {"label": "Standard Novel (~80,000-100,000 words)", "chapters": 32, "words_per_chapter": 2700},
    "long_novel": {"label": "Long Novel (~100,000-120,000 words)", "chapters": 40, "words_per_chapter": 2800},
    "epic": {"label": "Epic Novel (~120,000+ words)", "chapters": 50, "words_per_chapter": 3000},
    "micro_chapters": {
        "label": "Micro-chapter Novel (~40,000 words, 80 chapters x ~500 words, 5 acts)",
        "chapters": 80,
        "words_per_chapter": 500,
        "acts": 5,
    },
}
DEFAULT_LENGTH_CATEGORY = "novel"

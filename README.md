# ghost_writer

A multi-agent pipeline that writes a full novel with a local LLM and exports
a Kindle/KDP-ready `.docx`.

## Architecture

All agents call the same local model (Gemma 26B-A4B, MoE, served via
llama.cpp) through one HTTP endpoint, differentiated only by system prompt.
No cloud calls, no API keys.

```
Researcher -> World Builder -> Character Builder -> Outliner -> Pacing Critic (outline)
                                                                        |
                                                                        v
   for each chapter:
     Author (persona voice) -> Draft
        -> Editor (prose pass)
        -> Continuity Checker + Voice Consistency Checker
        -> Rewrite (only if issues found)
        -> Copy Editor (grammar/typos)
        -> Final
        -> Character Manager / World Manager (update memory)
     -> next chapter

   after all chapters:
     Pacing Critic (full manuscript) -> pacing_report.md
     Blurb Agent -> listing_copy.json (titles, back-cover blurb, synopsis)
     DOCX Export -> <slug>.docx
```

Everything is persisted in a per-book `story_bible.json` (characters, world,
outline, chapter drafts/finals, continuity notes), so the pipeline is
resumable: re-running `write` with the same title picks up wherever it
stopped (already-researched topics, already-outlined chapters, already
finalized chapters are all skipped).

### Author personas

The Author agent is not one fixed voice - it's staffed from a roster of
genre personas (`ghostwriter/agents/personas.py`): thriller, sci-fi, horror,
comedy, mystery, YA, romance, fantasy, noir/crime, historical, literary.
`select_personas_for_genre`
matches your `--genre` string against each persona's keywords, so e.g.
`--genre "sci-fi horror"` puts both the sci-fi and horror personas on the
same chapter, with one blended system prompt asking them to collaborate
into a single coherent voice rather than switching narrators. You can also
force a specific roster with `--personas scifi,comedy`. Run
`python -m ghostwriter.cli list-personas` to see the full roster.

### Retrieval / continuity

`ghostwriter/memory/retriever.py` is a small dependency-free BM25 index over
the story bible (characters, world entries, research notes, prior chapters).
The Author, Continuity Checker, and Voice Checker all query it for grounding
instead of stuffing the entire book into every prompt.

### Web research

The Researcher agent can reach the actual internet: it proposes search
queries from the book's genre/premise, runs them through DuckDuckGo's HTML
endpoint (no API key), fetches the top pages, and asks the model to write
notes grounded in that real text (`ghostwriter/tools/web_search.py`). If the
network is unavailable, it falls back automatically to the model's own
knowledge, so the pipeline still works fully offline. Controlled under the
`research:` block in `config.yaml` (`web_enabled`, `max_queries`,
`results_per_query`, `pages_per_query`).

### Series (multi-book)

Pass `--series "Name" --book-number N` to `write` and a **series bible**
(`ghostwriter/memory/series_bible.py`) sits above the per-book `story_bible.json`,
persisted at `series/<series-slug>/series_bible.json`:

- Book N's story bible is **seeded** from the series bible's canon: established
  characters, world entries, and research notes are copied in as a starting
  point, and a `series_recap` (synopses of prior books) is injected into the
  Outliner and Author prompts so book N doesn't contradict or blindly
  re-introduce what already happened. The World Builder and Character Builder
  still run once per book, but are told what's already established so they
  only add genuinely new cast/setting this book needs, rather than skipping
  entirely or duplicating canon.
- The persona roster picked for book 1 is locked in and reused for later
  books automatically (voice consistency across the series), unless you pass
  `--personas` explicitly to override it.
- Once book N is finalized, its (possibly updated/expanded) characters,
  world entries, and research notes are merged back into the series bible,
  along with a full, spoiler-complete chapter-by-chapter synopsis
  (`StoryBible.full_synopsis_brief()`) - so book N+1 inherits everything.
  This is deliberately *not* the back-cover blurb's `short_synopsis`, which
  is written spoiler-light on purpose for the KDP listing and would leave
  book N+1 outlining against an incomplete picture of how book N ended.
- Series project directories are namespaced (`<series-slug>__<book-slug>`)
  so a book title colliding with a standalone project or another series
  can't cause the wrong bible to load and get merged into series canon.
- Continuity checking still runs per-book via the same BM25 retriever
  (`memory/retriever.py`), which also indexes the injected `series_recap` -
  full prior-book manuscripts are intentionally *not* re-indexed per chapter,
  to keep continuity-check latency reasonable on this hardware.
- Caveat: characters/world/research accumulate across a series and are
  injected into every chapter's Author prompt in full (uncondensed). On this
  hardware's 16k context window, a long-running series with many books could
  eventually crowd out room for the chapter itself - not yet hit in testing,
  but worth watching (e.g. `len(bible.characters_brief().split())` growth)
  if you take a series past a couple of books.

```
python -m ghostwriter.cli write --series "Kessler Cycle" --book-number 1 ^
    --title "The Signal Between Stars" --genre "sci-fi thriller" ^
    --premise "..." --chapters 20

python -m ghostwriter.cli write --series "Kessler Cycle" --book-number 2 ^
    --title "The Static After" --genre "sci-fi thriller" ^
    --premise "Six months later, the signal returns." --chapters 20
```

## Setup

1. Model files (already in place):
   - `D:\code\models\gemma-4-26B-A4B-it-UD-Q4_K_M.gguf` (main model)
   - `D:\code\models\gemma-4-26B-A4B-it-Q4_0-MTP.gguf` (MTP draft head, speculative decoding)
   - llama.cpp CUDA build: `D:\code\llama-b9827-bin-win-cuda-13.3-x64\llama-server.exe`
2. Install Python deps:
   ```
   pip install -r requirements.txt
   ```
3. Check/adjust `config.yaml` if your paths differ. Notes on the GPU settings:
   the RTX 3050 has 6GB VRAM and the model is a 26B MoE with ~4B active
   params per token, so `gpu_layers: 999` + `n_cpu_moe: 999` keeps
   attention/shared layers on the GPU and pushes the (mostly-idle-per-token)
   MoE expert weights to CPU RAM - this fits the model without OOMing and
   keeps the hot path fast. `reasoning: "off"` is also required for this
   model: it's a thinking model, and left on it burns the whole `max_tokens`
   budget on hidden `<think>` content before ever writing the actual chapter
   or JSON, coming back empty. Turning it off also roughly halves latency.

## Running

**Terminal 1** - start the model server (leave running):
```
powershell -File scripts/start_llm_server.ps1
```

**Terminal 2** - write a book:
```
python -m ghostwriter.cli write ^
    --title "The Signal Between Stars" ^
    --genre "sci-fi thriller" ^
    --premise "A comms officer intercepts a signal that shouldn't exist, and tracing it unravels a conspiracy back on Earth." ^
    --chapters 20 --words-per-chapter 2000
```

Output lands in `projects/<slug>/`:
- `story_bible.json` - full state (resumable)
- `<slug>.docx` - the manuscript, Kindle-formatted (Heading 1 per chapter,
  page breaks, justified body text, no page-number headers/footers since
  Kindle reflows text). KDP/Kindle Create builds its navigable TOC from
  these Heading 1 styles automatically.
- `pacing_report.md` - structural critique of the finished manuscript
- `listing_copy.json` - title options, back-cover blurb, short synopsis

If `--series` was used, `series/<series-slug>/series_bible.json` holds the
shared canon (characters, world, research, per-book synopses) - see
[Series (multi-book)](#series-multi-book) above.

Interrupt anytime (Ctrl+C) - rerunning the same `write` command resumes from
the last completed stage/chapter.

## Smoke test (no LLM required)

Exercises the story bible, retriever, personas, and docx export with fixture
data, to catch plumbing bugs without waiting on model inference:
```
python scripts/smoke_test.py
```

## Live test (server must be running)

Exercises one real Researcher call (with web search) and one real Author
call against the actual model, to confirm end-to-end LLM wiring:
```
python scripts/live_test.py
```

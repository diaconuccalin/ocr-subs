# subs/sol — bitmap subtitles → SRT

`ocr_subs.py` turns a folder of subtitle *images* (bitmap subs ripped from a disc,
one JPEG per cue) into a timed `.srt`. It runs two ways from the one file: as a
command line, and inside a browser tab under Pyodide, from the page in this same
directory. Everything else here is per-film data.

## No LLM is involved anywhere

Worth stating plainly, because "OCR + text correction" invites the assumption.
The only recognition step is **tesseract** (classical LSTM OCR) — the local binary
on the command line, the same engine compiled to WebAssembly in the browser.
Steps 4, 5 and 6 below are pure `re`, `json` and `difflib`. The `corrections.json`
sidecars are **hand-written by a person**, not generated. `ocr_subs.py` imports
only stdlib plus `numpy`, `PIL`, `scipy`; there are no API keys and no provider
SDKs anywhere in the project, and the page makes no request to anything but its
own origin. Do not add an LLM step without asking — the point of the design is
that every transformation is inspectable and deterministic.

## Environment

- `tesseract 4.1.1` / leptonica 1.82.0 for the command line, invoked as a subprocess.
- `numpy 1.26.4`, `scipy 1.10.1` (`ndimage`), `pillow 10.2.0`.
- `tessdata/` holds `eng.traineddata` + `por.traineddata` for the command line.
  Installing language data system-wide needs root, so `setup_tessdata` (`:410`)
  points `TESSDATA_PREFIX` at this local dir — but only if the env doesn't already
  set it.
- **`tessdata/configs` is load-bearing.** `run_tesseract` (`:227`) passes `tsv` as a
  tesseract *config name*, which tesseract resolves as `$TESSDATA_PREFIX/configs/tsv`.
  Without it tesseract writes a plain `.txt`, **exits 0**, and `read` then fails on a
  `.tsv` that was never written — for every image. It is currently a symlink into
  `/usr/share/tesseract-ocr/4.00/`, which is why `tessdata/` is not committed.
  `setup_tessdata` checks for this at startup and says so plainly (`:428`).
- The browser gets its own copies of all of the above from `vendor/`, and needs
  none of this installed.

## Running it

```sh
python3 ocr_subs.py --dir "De Sol a Sol" --srt "2024 De Sol a Sol.srt"   # fill a template
python3 ocr_subs.py --dir "Vida Dentro"                                   # build from filenames
python3 ocr_subs.py --dir taxonomia --lang por --dry-run                  # inspect, write nothing
```

`--srt` is resolved relative to `--dir`. Output defaults to `<template>.filled.srt`
with a template, `<dirname>.srt` without. Other flags: `--out`, `--lang`, `--jobs`,
`--corrections` (default `corrections.json`, read from `--dir`), `--no-corrections`.

## The input contract

Every image filename encodes the cue's time range; this is the join key for the
whole program.

```
0_04_29_960__0_04_33_719_2051018422813022638342156.jpeg
└ 0:04:29.960 ┘└ 0:04:33.719 ┘└ ignored id ┘
```

`NAME_RE` (`:39`) parses it; `parse_timestamp` (`:115`) renders the canonical SRT
line `00:04:29,960 --> 00:04:33,719`.

## Shape of the module

Three functions are the seam both front ends drive, so that the browser has no
code path of its own:

- `plan` (`:437`) — steps 1 and 2. Everything decided before an image is read.
- `transcribe` (`:494`) — steps 3 to 5, a generator yielding one `Cue` (`:104`) per cue.
- `render_srt` (`:535`) — step 6.

`main` (`:541`) is a thin consumer of those three. `Abort` (`:88`) subclasses
`SystemExit`, so `raise Abort(...)` prints and exits 1 on the command line exactly
as a bare `SystemExit` did, while the browser can catch it and tell a bad input
apart from a crash. Warnings that the command line prints to stderr go through a
`report` callback (`to_stderr`, `:98`) so the page can show them instead.

**When changing any of this, the test that matters is that the command line's
stdout, stderr and exit code stay byte-identical across all eleven film dirs.**

## Pipeline

### Step 1 — collect the images (`collect`, `:341`)

Globs `*.jpeg`/`*.jpg`, maps timestamp-line → path. Warns about unparseable
filenames; **hard-fails** if two images claim the same range (`:351`) — that would
silently drop a cue.

### Step 2 — decide the cue list (`plan`, `:437`)

- **With a template**: it supplies cue order and numbering. Timestamp lines are
  extracted (`srt_timestamps`, `:366`) and cross-checked **both directions** — any
  SRT entry without an image, or image without an entry, aborts (`:470`). That
  check is why a mis-parsed filename can't quietly place a subtitle at the wrong time.
- **Without one**: cue list is the filenames sorted chronologically (`sort_key`,
  `:404`), numbered 1..N by `build`.

### Step 3 — OCR each image (`ocr`, `:279`)

1. **`load_ink`** (`:126`) — grayscale, threshold at `INK_THRESHOLD` → boolean ink mask.
2. **`row_bands`** (`:132`) — runs of inked rows = rendered text lines. Bands under
   `MIN_BAND_RATIO` of the tallest are speckle. The band count is the *expected*
   line count, used by step 5.
3. **`despeckle`** (`:150`) — removes JPEG dirt that tesseract reads as a stray word.
   Per line: label connected components, treat those ≥ `GLYPH_RATIO` of line height
   as letters, take their x-span, then *iteratively* grow that span over nearby
   small components (within `PUNCT_GAP` line heights) so punctuation survives — the
   loop repeats because each dot of `...` only reaches its neighbour. Blank the rest.
4. **`render`** (`:199`) — mask back to PNG, downscaled so a line is ~`TARGET_LINE_PX`
   tall. Sources are 8–11k px wide, far past what tesseract reads well; measuring on
   the *ink* rather than the image makes this resolution-independent.
5. **The two-render trick** — some films use a drop-shadow display face that knocks
   white gouges out of its own strokes, shredding letters after thresholding.
   `close_gouges` (`:214`) morphologically closes the mask to seal them (iterated 3×3
   instead of a big disk: same result, ~30× faster). But closing also thickens
   ordinary strokes enough to turn `0` into `8`, so **both** renders are OCR'd and the
   higher mean word-confidence wins, ties to plain (`:299`).
6. **`read`** (`:253`) — parses **TSV**, not plain text, for two reasons: per-word
   confidences (needed for the choice above) and block/paragraph/line columns, so
   two-line cues come back as two lines.
   - Where that TSV comes from is the module's one pluggable point: `TESSERACT`
     (`:250`) defaults to `run_tesseract` (`:227`), which runs
     `tesseract - <base> -l <lang> --psm 6 tsv` as a subprocess. `--psm 6` (uniform
     block) is the mode that preserves line breaks. WebAssembly has no subprocesses,
     so `worker-py.js` rebinds `TESSERACT` to a call into tesseract.js; nothing else
     in the pipeline can tell.
7. **`clean`** (`:303`) — runs inside `read`, i.e. on *both* renders, but confidence is
   computed on tesseract's raw words, so cleaning never influences which render wins.
   - Folds the four curly quotes to straight (`QUOTE_MAP`, applied before splitting).
   - Per line: `" ".join(line.split())`; empty lines dropped entirely.
   - **English only** (`lang == "eng"`): the font makes `I`, `l`, `|` and a dotless
     `i` near-identical. Four subs, all anchored by `(?<![^\s-])` — "preceded by
     whitespace, a hyphen, or start of string". The hyphen is there because a leading
     `-` marks the second speaker in a two-line cue and shouldn't detach the word.
     - Three run one way (`:315-317`): a bare `|` or `l` is the pronoun "I", as is
       either one carrying a contraction (`l'm`) or opening `If`.
     - One runs the other way (`:321`): a lowercase `i` that has lost its dot comes
       back as `t` or `l`, and neither `ts` nor `ls` is a word standing alone. The
       trailing `(?!')` leaves a token carrying a contraction alone, since `is'` is
       not English either and the shape is then probably something else entirely.
     Verified: `l am` → `I am`, `l'm` → `I'm`, `lf you` → `If you`, `- l said` →
     `- I said`, `| know` → `I know`, `This ts not a person` → `… is …`,
     `- It ls nothing` → `- It is nothing`; `la casa`, `tsunami`, `the lst of many`,
     `he sits`, `Ts and Ls` and `call l up`'s neighbours untouched.
     Skipped for `por`, where those shapes are real words.
   - **How thin the evidence has to be before a rule is worth adding.** The `ts`/`ls`
     sub was measured before it was written: it fires **3 times in ~1,240 cues** of
     raw OCR across the six English films, and all three are right. That is the bar —
     the other candidates found in the same sweep (`/ast` → `last`, mid-sentence
     `In` → `in`, bare `1` → `I`) were each correct too, and were all rejected for
     resting on one or two observations. See the corrections analysis below.

### Step 4 — corrections sidecar

- **Load** (`load_corrections`, `:322`): `{}` if absent, so the default sidecar is
  opt-out-by-absence, not an error. **Every key starting with `_` is dropped** (`:327`)
  — that's what makes the sidecars self-documenting (`_comment`, `_verified`,
  `_outline_renders`, per-cue notes). Path is `image_dir / corrections_name`.
- **Validate** (`:481-486`): keys matching no image → warning, not fatal.
- **Apply** (`:511-514`): only when `corrections[stamp] != text`, so an entry that
  already matches the OCR is a silent no-op and isn't listed as applied. Happens
  **before** the step-5 checks, so a correction of the right shape silences them.
- **Diff** (`word_diff`, `:330`): whitespace-split + `difflib.SequenceMatcher`, skip
  `equal`, `∅` for an empty side. `('utiful?' -> 'it beautiful?')`, `('b' -> '∅')`.
  Because `split()` ignores newlines, a correction that only changes *line breaks*
  is still applied but prints an empty diff.
- **Report** to stderr after the loop (`:603-607`), keyed by `stamp[:12]`.
- **The page never does any of this** — it always passes `no_corrections=True`.
  Step 4 is a command-line feature, and the five films with a sidecar come out
  visibly rougher in the browser.

### Why the sidecars cannot be automated

All 125 word-level changes across the five sidecars were classified once, and the
answer is worth keeping so nobody re-derives it — or reaches for a language model
to do it.

- **About half restore glyphs that were never rendered.** The source bitmap for
  one *De Sol a Sol* cue literally reads `dust has been takin  hold of it.`, with
  a gap where the `g` should be. It is not our pipeline: that image is a single
  contiguous ink band and `despeckle` drops nothing from it. The characters the
  corrections put back follow ordinary letter frequency (`a` 16%, `e` 11%, `o` 7%),
  so it is general dropout in the rips, not some letter class a rule could target —
  descenders are only 9% of them.
- **About a quarter are word-level reconstruction** needing meaning, not sight:
  `'fallonto ofa_ Ile'` → `'fall on top of a pile'`. That is the LLM step this
  project does not have.
- **Ten per cent are speaker labels** whose insides are destroyed the same way:
  `'[D- - ra]'` → `'[Dandara]'`. A per-film vocabulary would fix them, which is
  exactly what a sidecar already is.
- **Genuine character confusions are scarce and scattered.** The most frequent
  single-character substitution in the whole corpus appears three times — and all
  three are inside one cue. Only the `ts`/`ls` family recurred across films, which
  is why it is the only one that became a rule.

### Step 5 — warnings

One check per cue (`:519-529`), on the text *after* corrections: `not text` →
"OCR'd to nothing". It is **purely informational and never aborts**. The only
fatal conditions are elsewhere: duplicate images (`:351`), no images (`:448`),
template mismatch (`:470`).

**There used to be a second check** comparing the band count from step 3.2
(`want`) with `len(text.splitlines())` (`got`), and it is worth knowing why it
is gone, because the idea is tempting enough to be reinvented.

A band is a run of inked rows, and no fully blank row can fall inside a single
line of text — so the band count can never *overcount* lines, only undercount
them, which it does whenever the leading is tight enough that a descender meets
the ascender below it and two real lines merge into one band. That made
`got > want` a false alarm every time: 44 of the 46 line-count warnings across
the eleven films were this direction, 38 in `o_que` alone, all on cues that were
transcribed perfectly.

The other direction, `got < want`, was sound — `onde` has a cue with two clear
bands of ink that OCR'd to the two characters `AR`, and only this caught it. It
was dropped anyway, on the grounds that two true positives in eleven films did
not pay for the noise they arrived with. **The cost is real: a cue that returns
a little bad text now passes silently**, since "OCR'd to nothing" only fires on
an empty result. `want` and `got` are still carried on the `Cue` record, so
reinstating a check means writing the condition, not re-deriving the data.

### Step 6 — write (`render_srt`, `:535`)

`fill` (`:377`) with a template — keeps each block's number and timestamp, swaps the
`[sub_duration]` placeholder for the OCR'd body. `build` (`:395`) without — emits
`N / timestamp / text` numbered chronologically. Line endings match the template
(CRLF if it had them, CRLF by default when building fresh) because SRT players are
picky. `--dry-run` prints each transcription plus a speck count and returns early.

## `--jobs`

`transcribe(..., workers=N)` maps `ocr` over a `ThreadPoolExecutor` (`:506`). Safe:
`ocr` is a pure function of `(path, lang)` with its own `TemporaryDirectory`, and no
module state is mutated after startup. `Executor.map` yields in *input* order, so
corrections and warnings still come out in strict cue order — **`--jobs 4` must
produce byte-identical output to `--jobs 1`, which is how to test it**. It also sets
`OMP_THREAD_LIMIT=1` (`:504`), because tesseract 4.x uses OpenMP internally and
several multithreaded copies fight over the same cores. Measured on taxonomia:
~2.3 min at `--jobs 1`, ~45 s at `--jobs 4`. The browser does not use this.

## The page

Served from the repo root (not `/docs`) so that `worker-py.js` fetches
`./ocr_subs.py` — the very same file the command line runs, rather than a copy that
could drift.

```
main thread  app.js        the UI, and tesseract.js, which does the recognition
     │                     (here rather than in a worker of its own: tesseract.js
     │                     starts its own worker, and nesting those is recent)
     ▼
  worker-py.js             Pyodide running ocr_subs.py, blocking on each OCR call
```

`read` is synchronous and tesseract.js is promise-based, so the Python worker really
does block: it writes the render into a `SharedArrayBuffer`, posts to the page, and
`Atomics.wait`s. That is why the worker exists (blocking the main thread is not
allowed, and would freeze the UI) and why the page must be cross-origin isolated —
which GitHub Pages cannot arrange, so `coi-serviceworker.js` does it and the page
reloads once on a first visit.

Gotchas found the hard way:

- `TextDecoder` refuses a view onto shared memory in Chrome, so bytes coming back
  out of the `SharedArrayBuffer` must be `.slice`d, not `.subarray`d.
- Files are written into Pyodide's virtual filesystem under `/work/<folder>/`, so
  `plan` gets a real directory and `default_out` matches the command line's name.
  Names from the browser are reduced to a basename before use.
- `showSaveFilePicker` must be the **first** statement in the click handler, before
  any `await`, or the click no longer counts as the gesture that permits a dialog.

### Verified

- Pyodide's numpy/scipy/Pillow produce **byte-identical renders** to the local ones,
  so steps 3.1–3.5 are exact and the only source of divergence is the engine.
- Browser output equals `python3 ocr_subs.py --no-corrections` exactly on
  *De Sol a Sol*; on *Vida Dentro* and *taxonomia* it differs on **1–2 % of lines**,
  because tesseract.js is a 5.x engine and the local binary is 4.1.1. The differences
  are concentrated in already-degraded cues and go both ways.
- Speed is close to native: 0.40 s/cue on *Vida Dentro*, 0.79 s/cue on *taxonomia*
  (242 cues in 3.2 min). Sharding across several Pyodide workers was measured as not
  worth the ~300 MB per worker it costs; if that changes, shard the cue list.

## Tuning constants (`:45-95`)

`TARGET_LINE_PX 32`, `INK_THRESHOLD 128`, `MIN_BAND_RATIO 0.3`, `GLYPH_RATIO 0.3`,
`PUNCT_GAP 0.5`, `SHADOW_CLOSE_FRAC 0.06`. All are expressed relative to the measured
line height so they hold across films shot at different resolutions — keep it that way.

## Known gotchas

- **`:316` is dead code.** `(?<![^\s-])[|l](?=')` never fires: the preceding sub's
  lookahead `(?![^\s'])` already admits a following apostrophe, so `l'm` is `I'm`
  before line 316 runs. Harmless, just redundant.
- **A bare pronoun followed by punctuation is not corrected** — `l.` stays `l.`,
  because `.` fails that same lookahead. Same for `l,` and `l?`. Consistent with the
  module's stated stance ("deliberately tiny"), but a real gap if a cue ends on `I`.
- `clean`'s fixes are English-only by design; don't extend them to `por` casually.

## Per-film data

Each film dir holds the JPEGs, optionally a template `.srt` (the ones containing
`[sub_duration]`) and optionally `corrections.json`. None of it is committed.

| dir | imgs | template | corrections | lang |
|---|---|---|---|---|
| De Sol a Sol | 22 | yes | yes | eng |
| Ruvinho | 101 | yes | – | eng |
| Vida Dentro | 177 | – (built from filenames) | yes | eng |
| dizemos | 255 | yes | – | por |
| fragmentary | 68 | yes | yes | eng |
| o_que | 121 | yes | – | eng |
| on_the_sea | 68 | yes | – | por |
| onde | 104 | yes | yes | eng |
| ora_esta | 93 | yes | – | por |
| taxonomia | 242 | yes | yes | por |
| thanksgiving | 0 | – | – | source only |

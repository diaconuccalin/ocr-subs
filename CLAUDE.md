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
  Installing language data system-wide needs root, so `setup_tessdata` (`:457`)
  points `TESSDATA_PREFIX` at this local dir — but only if the env doesn't already
  set it.
- **`tessdata/configs` is load-bearing.** `run_tesseract` (`:269`) passes `tsv` as a
  tesseract *config name*, which tesseract resolves as `$TESSDATA_PREFIX/configs/tsv`.
  Without it tesseract writes a plain `.txt`, **exits 0**, and `read` then fails on a
  `.tsv` that was never written — for every image. It is currently a symlink into
  `/usr/share/tesseract-ocr/4.00/`, which is why `tessdata/` is not committed.
  `setup_tessdata` checks for this at startup and says so plainly (`:475`).
- `words.txt` beside the script is the English word list the suspect report
  uses, from SCOWL by way of Aspell, with its copyright notice in the header.
  Absent is fine: the report is skipped.
- The browser gets its own copies of all of the above from `vendor/`, and needs
  none of this installed. `worker-py.js` writes `words.txt` into Pyodide's
  filesystem next to `ocr_subs.py`, which is where `LOCAL_WORDS` looks.

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

`NAME_RE` (`:42`) parses it; `parse_timestamp` (`:158`) renders the canonical SRT
line `00:04:29,960 --> 00:04:33,719`.

## Shape of the module

Three functions are the seam both front ends drive, so that the browser has no
code path of its own:

- `plan` (`:484`) — steps 1 and 2. Everything decided before an image is read.
- `transcribe` (`:537`) — steps 3 to 5, a generator yielding one `Cue` (`:117`) per cue.
- `render_srt` (`:535`) — step 6.

`main` (`:541`) is a thin consumer of those three. `Abort` (`:101`) subclasses
`SystemExit`, so `raise Abort(...)` prints and exits 1 on the command line exactly
as a bare `SystemExit` did, while the browser can catch it and tell a bad input
apart from a crash. Warnings that the command line prints to stderr go through a
`report` callback (`to_stderr`, `:111`) so the page can show them instead.

**When changing any of this, the test that matters is that the command line's
stdout, stderr and exit code stay byte-identical across all eleven film dirs.**

## Pipeline

### Step 1 — collect the images (`collect`, `:388`)

Globs `*.jpeg`/`*.jpg`, maps timestamp-line → path. Warns about unparseable
filenames; **hard-fails** if two images claim the same range (`:398`) — that would
silently drop a cue.

### Step 2 — decide the cue list (`plan`, `:484`)

- **With a template**: it supplies cue order and numbering. Timestamp lines are
  extracted (`srt_timestamps`, `:413`) and cross-checked **both directions** — any
  SRT entry without an image, or image without an entry, aborts (`:517`). That
  check is why a mis-parsed filename can't quietly place a subtitle at the wrong time.
- **Without one**: cue list is the filenames sorted chronologically (`sort_key`,
  `:451`), numbered 1..N by `build`.

### Step 3 — OCR each image (`ocr`, `:321`)

1. **`load_ink`** (`:171`) — grayscale, threshold at `INK_THRESHOLD` → boolean ink mask.
2. **`row_bands`** (`:177`) — runs of inked rows = rendered text lines. Bands under
   `MIN_BAND_RATIO` of the tallest are speckle. The band count is the *expected*
   line count, used by step 5.
3. **`despeckle`** (`:195`) — removes JPEG dirt that tesseract reads as a stray word.
   Per line: label connected components, treat those ≥ `GLYPH_RATIO` of line height
   as letters, take their x-span, then *iteratively* grow that span over nearby
   small components (within `PUNCT_GAP` line heights) so punctuation survives — the
   loop repeats because each dot of `...` only reaches its neighbour. Blank the rest.
4. **`deblob`** (`:212`) — removes solid blots lying *on* the words, which
   `despeckle` keeps because they are neither small nor far from the text. A
   mark fatter than `BLOB_STROKES` times the frame's median ink thickness is
   dropped whole. **This one trades losses for wins** — see its own section
   below before touching it.
5. **`render`** (`:241`) — mask back to PNG, downscaled so a line is ~`TARGET_LINE_PX`
   tall. Sources are 8–11k px wide, far past what tesseract reads well; measuring on
   the *ink* rather than the image makes this resolution-independent.
6. **The two-render trick** — some films use a drop-shadow display face that knocks
   white gouges out of its own strokes, shredding letters after thresholding.
   `close_gouges` (`:256`) morphologically closes the mask to seal them (iterated 3×3
   instead of a big disk: same result, ~30× faster). But closing also thickens
   ordinary strokes enough to turn `0` into `8`, so **both** renders are OCR'd and the
   higher mean word-confidence wins, ties to plain (`:341`).
7. **`read`** (`:295`) — parses **TSV**, not plain text, for two reasons: per-word
   confidences (needed for the choice above) and block/paragraph/line columns, so
   two-line cues come back as two lines.
   - Where that TSV comes from is the module's one pluggable point: `TESSERACT`
     (`:292`) defaults to `run_tesseract` (`:269`), which runs
     `tesseract - <base> -l <lang> --psm 6 tsv` as a subprocess. `--psm 6` (uniform
     block) is the mode that preserves line breaks. WebAssembly has no subprocesses,
     so `worker-py.js` rebinds `TESSERACT` to a call into tesseract.js; nothing else
     in the pipeline can tell.
8. **`clean`** (`:346`) — runs inside `read`, i.e. on *both* renders, but confidence is
   computed on tesseract's raw words, so cleaning never influences which render wins.
   - Folds the four curly quotes to straight (`QUOTE_MAP`, applied before splitting).
   - Per line: `" ".join(line.split())`; empty lines dropped entirely.
   - **English only** (`lang == "eng"`): the font makes `I`, `l`, `|` and a dotless
     `i` near-identical. Four of the subs are anchored by `(?<![^\s-])` — "preceded by
     whitespace, a hyphen, or start of string". The hyphen is there because a leading
     `-` marks the second speaker in a two-line cue and shouldn't detach the word.
     - Three run one way (`:358-360`): a bare `|` or `l` is the pronoun "I", as is
       either one carrying a contraction (`l'm`) or opening `If`.
     - One runs the other way (`:364`): a lowercase `i` that has lost its dot comes
       back as `t` or `l`, and neither `ts` nor `ls` is a word standing alone. The
       trailing `(?!')` leaves a token carrying a contraction alone, since `is'` is
       not English either and the shape is then probably something else entirely.
     Verified: `l am` → `I am`, `l'm` → `I'm`, `lf you` → `If you`, `- l said` →
     `- I said`, `| know` → `I know`, `This ts not a person` → `… is …`,
     `- It ls nothing` → `- It is nothing`; `la casa`, `tsunami`, `the lst of many`,
     `he sits`, `Ts and Ls` and `call l up`'s neighbours untouched.
     Skipped for `por`, where those shapes are real words.
   - **Three more marks-read-as-characters subs**, added after `deblob` and aimed at
     the dirt it cannot reach — the marks fused to a glyph, which no image step can
     lift out. An underscore between letters is a mark; so is a capital inside a
     lower-case word (`tWenty`), except that words beginning with a capital do this
     legitimately (`McDonald`, `YouTube`), so only lower-case-initial words are
     touched. `CAMEL_WORDS` spares the handful that start lower-case and carry a
     capital on purpose — `iPhone`, `eBay`, `iOS`. That is an exception list, not
     a dictionary: anything not on it is lower-cased, so a new brand name will be
     flattened until someone adds it.
   - **The apostrophe sub is the fussy one**, because most apostrophes are real. One
     between two letters survives only if what follows it is a contraction or a
     possessive — `APOSTROPHE_TAILS`, every entry of which was found in these films
     bar the last four. A capital after it means a name (`O'Brien`) and nothing after
     it means a plural possessive (`guys'`); requiring a lower-case letter after the
     apostrophe leaves both alone, and a single letter fenced by apostrophes is
     read as an idiom — `rock'n'roll`, `guns'n'roses`, `Toys'R'Us` — whichever
     letter it is, so neither of its apostrophes is touched. So
     `about'tWenty` and `Scope'screen` lose theirs while `don't`, `cinema's`,
     `y'all` and `ma'am` keep theirs.
   - **How thin the evidence has to be before a rule is worth adding.** The `ts`/`ls`
     sub was measured before it was written: it fires **3 times in ~1,240 cues** of
     raw OCR across the six English films, and all three are right. The three above
     were measured the same way and change **6 lines in 973** — 5 right, 1 garbage
     either way, none wrong. That is the bar; candidates found in the same sweep
     (`/ast` → `last`, mid-sentence `In` → `in`, bare `1` → `I`) were each correct
     too and were rejected for resting on one or two observations. See the
     corrections analysis below.

### `deblob`: the dirt `despeckle` does not catch

Some rips carry solid marks lying *across* the words — scratches and blots, not
the scatter of specks `despeckle` was written for. They are big enough and close
enough to the type that its size-and-distance test keeps them, and tesseract
reads them as letters: a wedge over "mud" comes back as `Mud`, a slash beside
`this.` becomes `this?`, `my uncle` becomes `niy$dncle`. The source bitmaps are
perfectly legible; the text is only wrong because of what is lying on it.

`deblob` (`:212`) removes them, on a thickness test: a face has one stroke
width, so a mark far fatter than a stroke is not a glyph. For each connected
mark take the largest distance-to-background anywhere in it, and drop the mark
whole if that exceeds `BLOB_STROKES` times the median of that distance over all
ink. The threshold came from the gap on `test_extracted`:

| on `test_extracted` | ratio |
|---|---|
| thickest real glyph (a capital `M`, `N` or `W` junction) | 3.75x |
| thinnest piece of dirt | 5.15x |

**This is a judgement call that was taken deliberately, not a clean win.** Over
the eleven films it changes 34 cues of 1251: **16 better, 7 worse, 1 mixed**,
plus 10 that are garbage either way. Every one of the 34 was read against its
source bitmap, so those labels are checked rather than guessed — though 5 of the
16 are only partly fixed, closer to the truth and still wrong.

The losses are the quiet kind and that is the cost being accepted: they turn
*correct* text into plausible wrong text — `48ºC` into `458ºC`, `consiste` into
`consis`, `Maravilha` into `Aaravilha` — which reading the output will not
reliably catch, where the wins turn visible garbage into words. **Read what
comes out; do not trust a clean-looking run.** `--dry-run` prints a
`[N blob removed]` note on every cue this touched, which is the list to check
first.

The measurement that killed it: the median ink half-thickness the ratio is taken
against runs **2.0 px in `dizemos` and `o_que`, 3.6 in `ora_esta`, 4.0 in
`test_extracted`, 8.5 in `taxonomia`**, because the films are rendered at very
different sizes. The ratios then overlap completely — the capital `M` of
*Maravilha* scores 5.50x, *fatter* than the dirt at 5.15x that the threshold was
built to catch — and at 2 px the ratio is quantised into steps of 0.5, so it is
noisiest exactly where it does most damage. No cutoff separates those two
populations, so this is not a constant that wants retuning.

**Do not try to improve it by renormalising.** The median stroke is not
scale-invariant, and everything else here is measured against line height for
exactly that reason, so that repair looks obvious. It was tried, and it fails.
Against a set of marks known to be dirt (removing them fixed the text) and marks
known to be glyphs (removing them broke it):

| normaliser | known dirt | known glyphs |
|---|---|---|
| fatness / median stroke | 5.15x and up | 4.48x – 8.50x |
| fatness / line height | 0.055 – 0.119 | 0.066 – 0.198 |
| slenderness, `area / fattest²` | 6.6 – 10.7 | 4.2 – 48.4 |

Every one of them overlaps. `ora_esta` carries a glyph that is fatter than all
the dirt *and* stubbier than all of it; `dizemos` has a capital `M` fatter than
the dirt the threshold was built for. Fatness and shape, alone or renormalised,
do not separate these two populations.

Combining fatness and slenderness separates all ten of those marks, and that
result is worthless: two free parameters fitted to ten hand-picked points. It
was measured properly afterwards and **it eats punctuation** — on the one film
it was designed for it changed 74 cues of 85, 56 of them by losing punctuation
alone, destroying 89 marks. A comma is fat-but-stubby by construction. The ten
marks never showed it because they were sampled as "thickest component per
image", which is always a blot or a capital and never a comma. Sampling the
population that cannot expose your failure is the whole lesson.

**What the artifact actually is.** Reading the source bitmaps for all 34 changed
cues: it is nearly always a *solid blob*, and the films differ in where it lands.
In `dizemos` and `ora_esta` the blobs sit in the counters of round letters — an
`o` with a filled-in hole. In `o_que` they are ink spatter and long diagonal
scratches. `test_extracted` gets wedges and slashes lying across the words.

**Which way it goes is decided by one thing:** whether the blob is a separate
connected component or fused to the glyph.

- **Separate** — a disc sitting inside the bowl of an `o` without touching it.
  Removing it leaves the letter whole, and the word comes back right. This is
  every one of the 16 fixes: `tomara`, `obrigado`, `minutos`, `falando de vocês`,
  `Chile` twice, `remember`.
- **Fused** — the blob touches the stroke, so it and the letter are one
  component, and removing it takes the letter too. This is every one of the
  breaks: `Maravilha` to `Aaravilha`, `your` to `yeu`, `consiste` to `consis`.

That is why no *thickness* threshold can work: a filled-in `o` and a blot of
dirt are not merely hard to tell apart, they are the same object. The question
that matters is not how fat a mark is but whether it is touching type, and
whole-component removal cannot ask it.

**Two films are rendered as hollow outline type** — `dizemos` in part, and the
display face on one `o_que` cue. There "thickness" measures the outline stroke
rather than the glyph, so the premise does not even apply. That `o_que` cue is
the one the threshold destroyed outright.

**How far to trust the 16:8.** The 34 were the complete set of changes over 1251
cues in eleven films that had no part in choosing 4.4 — not a sample, which is
what makes it worth more than the ten-mark exercise. But none of the 34 has a
sidecar entry, so there was no ground truth: the labels were one reading. Every
one was afterwards checked against the source bitmap. The 16 fixes all hold,
though 5 are partial — closer to the truth, still wrong. Of the 8 breaks, 7 hold
and one (`o_que`, `thd`→`and` while breaking three other words) is really mixed.
Call it **16 better, 7 worse, 1 mixed**, on a population of 34, which carries
wide error bars whichever way it is read.

Anything better needs a labelled set of marks drawn from every film, built
before any threshold is chosen, and judged on cues nobody used to design it —
and it needs to answer "is this mark touching type", which is the question that
actually decides the outcome. Until then `BLOB_STROKES` is a dial with a known
cost, and the cues it cannot reach are a `corrections.json` job.

### Step 4 — corrections sidecar

- **Load** (`load_corrections`, `:369`): `{}` if absent, so the default sidecar is
  opt-out-by-absence, not an error. **Every key starting with `_` is dropped** (`:374`)
  — that's what makes the sidecars self-documenting (`_comment`, `_verified`,
  `_outline_renders`, per-cue notes). Path is `image_dir / corrections_name`.
- **Validate** (`:528-533`): keys matching no image → warning, not fatal.
- **Apply** (`:558-561`): only when `corrections[stamp] != text`, so an entry that
  already matches the OCR is a silent no-op and isn't listed as applied. Happens
  **before** the step-5 checks, so a correction of the right shape silences them.
- **Diff** (`word_diff`, `:377`): whitespace-split + `difflib.SequenceMatcher`, skip
  `equal`, `∅` for an empty side. `('utiful?' -> 'it beautiful?')`, `('b' -> '∅')`.
  Because `split()` ignores newlines, a correction that only changes *line breaks*
  is still applied but prints an empty diff.
- **Report** to stderr after the loop (`:649-653`), keyed by `stamp[:12]`.
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

Two reports per cue, both on the text *after* corrections, both **purely
informational, never aborting**, and both naming the **cue number** rather than
the image, because a number is what somebody checking the result has in front of
them. The only fatal conditions are elsewhere: duplicate images (`:398`), no
images (`:495`), template mismatch (`:517`).

- `not text` → "cue N OCR'd to nothing".
- **The suspect-word report** (`suspects`, `:479`): a lower-case word of four
  letters or more that is not in `words.txt` but sits exactly one letter from a
  word that is → `cue 35 reads "clearty", perhaps "clearly"`. Lower-case only,
  so no proper noun is ever queried; exactly one candidate, so nothing anybody
  would have to guess at. English only — there is no other word list.

**It reports and never rewrites, and that is a measured decision rather than
caution.** Applying the same test as a correction was tried over the 973 lines
of English OCR: 31 words would change, about 8 rightly and 18 wrongly. Two
things sink it, neither of them fixed by the lower-case filter that does keep
proper nouns safe. `fragmentary` is Portuguese poetry with English subtitles, so
lower-case Portuguese gets "corrected" into English — `apenas` into `arenas`,
`futuro` into `future`, `leite` into `lite`. And no word list is the language:
`compressions` and `rarefactions` are ordinary English that this one lacks, and
rewriting them corrupts correct text. Meanwhile the errors worth catching are
mostly out of reach anyway — `dads` for `does` is a real word, `itis` for
`it is` is a split rather than a substitution.

As a *report* none of that bites: a wrong guess costs a line somebody skims.
**Expect noise on a bilingual film** — `fragmentary` produces 18 of the 24 flags
across the eleven films, most of them Portuguese flagged for not being English.

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

### Step 6 — write (`render_srt`, `:578`)

`fill` (`:424`) with a template — keeps each block's number and timestamp, swaps the
`[sub_duration]` placeholder for the OCR'd body. `build` (`:442`) without — emits
`N / timestamp / text` numbered chronologically. Line endings match the template
(CRLF if it had them, CRLF by default when building fresh) because SRT players are
picky. `--dry-run` prints each transcription plus a speck count and returns early.

## `--jobs`

`transcribe(..., workers=N)` maps `ocr` over a `ThreadPoolExecutor` (`:553`). Safe:
`ocr` is a pure function of `(path, lang)` with its own `TemporaryDirectory`, and no
module state is mutated after startup. `Executor.map` yields in *input* order, so
corrections and warnings still come out in strict cue order — **`--jobs 4` must
produce byte-identical output to `--jobs 1`, which is how to test it**. It also sets
`OMP_THREAD_LIMIT=1` (`:551`), because tesseract 4.x uses OpenMP internally and
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
  (242 cues in 3.2 min), measured before `deblob`. Sharding across several Pyodide
  workers was measured as not worth the ~300 MB per worker it costs; if that
  changes, shard the cue list.
- **`deblob` costs about 0.3 s a cue**, the distance transform being the whole of
  it, so it adds roughly half again to a run. Two things keep that down and both
  must hold if it is touched: the transform runs on the ink's bounding box rather
  than the frame, and it thresholds *before* labelling, so a clean frame — which
  most are — never pays for a component pass. Together those took it from 0.49 s
  to 0.32 s a cue with byte-identical output across all eleven films.

## Tuning constants (`:48-108`)

`TARGET_LINE_PX 32`, `INK_THRESHOLD 128`, `MIN_BAND_RATIO 0.3`, `GLYPH_RATIO 0.3`,
`PUNCT_GAP 0.5`, `SHADOW_CLOSE_FRAC 0.06`. All are expressed relative to the measured
line height so they hold across films shot at different resolutions — keep it that way.

## Known gotchas

- **`:359` is dead code.** `(?<![^\s-])[|l](?=')` never fires: the preceding sub's
  lookahead `(?![^\s'])` already admits a following apostrophe, so `l'm` is `I'm`
  before line 359 runs. Harmless, just redundant.
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

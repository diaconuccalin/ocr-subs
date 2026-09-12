# subs/sol — bitmap subtitles → SRT

`ocr_subs.py` turns a folder of subtitle *images* (bitmap subs ripped from a disc,
one JPEG per cue) into a timed `.srt`. It runs two ways from the one file: as a
command line, and inside a browser tab under Pyodide, from the page in this same
directory. Everything else here is per-film data.

## No LLM is involved anywhere

Worth stating plainly, because "OCR + text correction" invites the assumption.
The only recognition step is **tesseract** (classical LSTM OCR) — the local binary
on the command line, the same engine compiled to WebAssembly in the browser.
Steps 4 to 7 below are pure `re`, `json` and `difflib`. The `corrections.json`
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
- `words.txt` beside the script is the English word list, from SCOWL by way of
  Aspell, with its copyright notice in the header. Two things ask it: the
  suspect report, and `clean`'s de-accent sub. Absent is not an error, but it is
  not nothing either — the report is skipped *and* the de-accent sub stops
  firing, so the English films come out very slightly rougher.
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
`--corrections` (default `corrections.json`, read from `--dir`),
`--no-corrections`, and `--no-merge`, which keeps every image as its own cue
even where the rip split one subtitle across several.

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

Four functions are the seam both front ends drive, so that the browser has no
code path of its own:

- `plan` (`:1000`) — steps 1 and 2. Everything decided before an image is read.
- `transcribe` (`:1053`) — steps 3 to 5, a generator yielding one `Cue` (`:214`) per cue.
- `merge_repeats` (`:839`) — step 6, folding the cues the rip split.
- `render_srt` (`:1120`) — step 7.

`main` (`:1126`) is a thin consumer of those four. `Abort` (`:165`) subclasses
`SystemExit`, so `raise Abort(...)` prints and exits 1 on the command line exactly
as a bare `SystemExit` did, while the browser can catch it and tell a bad input
apart from a crash. Warnings that the command line prints to stderr go through a
`report` callback (`to_stderr`, `:175`) so the page can show them instead.

`transcribe` reads every image twice: a cheap pass for `film_line_height` before
the first cue, then the OCR. That is the one place where a cue's text depends on
the rest of the folder rather than on itself — see the merged-band section. Its
optional `progress(done, total)` reports that first pass; only the page uses it.

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

### Step 3 — OCR each image (`ocr`, `:431`)

1. **`load_ink`** (`:203`) — grayscale, threshold at `INK_THRESHOLD` → boolean ink mask.
2. **`row_bands`** (`:209`) — runs of inked rows = rendered text lines. Bands under
   `MIN_BAND_RATIO` of the tallest are speckle. The band count is the *expected*
   line count, used by step 5.
3. **`despeckle`** (`:255`) — removes JPEG dirt that tesseract reads as a stray word.
   Per line: label connected components, treat those ≥ `GLYPH_RATIO` of line height
   as letters, take their x-span, then *iteratively* grow that span over nearby
   small components (within `PUNCT_GAP` line heights) so punctuation survives — the
   loop repeats because each dot of `...` only reaches its neighbour. Blank the rest.
4. **`deblob`** (`:304`) — removes solid blots lying *on* the words, which
   `despeckle` keeps because they are neither small nor far from the text. A
   mark fatter than `BLOB_STROKES` times the frame's median ink thickness is
   dropped whole. **This one trades losses for wins** — see its own section
   below before touching it.
5. **`line_h`** (`:451`) — the median band height, which every constant here is
   measured against. A band taller than `LINE_MERGE_RATIO` times the film's own
   line height is not one line, so that frame falls back to the film's
   measurement (`film_line_height`, `:227`). See its own section below.
6. **`render`** (`:351`) — mask back to PNG, downscaled so a line is ~`TARGET_LINE_PX`
   tall. Sources are 8–11k px wide, far past what tesseract reads well; measuring on
   the *ink* rather than the image makes this resolution-independent.
7. **The two-render trick** — some films use a drop-shadow display face that knocks
   white gouges out of its own strokes, shredding letters after thresholding.
   `close_gouges` (`:366`) morphologically closes the mask to seal them (iterated 3×3
   instead of a big disk: same result, ~30× faster). But closing also thickens
   ordinary strokes enough to turn `0` into `8`, so **both** renders are OCR'd and the
   higher mean word-confidence wins, ties to plain (`:459`).
8. **`read`** (`:405`) — parses **TSV**, not plain text, for two reasons: per-word
   confidences (needed for the choice above) and block/paragraph/line columns, so
   two-line cues come back as two lines.
   - Where that TSV comes from is the module's one pluggable point: `TESSERACT`
     (`:402`) defaults to `run_tesseract` (`:379`), which runs
     `tesseract - <base> -l <lang> --psm 6 tsv` as a subprocess. `--psm 6` (uniform
     block) is the mode that preserves line breaks. WebAssembly has no subprocesses,
     so `worker-py.js` rebinds `TESSERACT` to a call into tesseract.js; nothing else
     in the pipeline can tell.
9. **`clean`** (`:463`) — runs inside `read`, i.e. on *both* renders, but confidence is
   computed on tesseract's raw words, so cleaning never influences which render wins.
   - Folds the four curly quotes to straight (`QUOTE_MAP`, applied before splitting).
   - Per line: `" ".join(line.split())`; empty lines dropped entirely.
   - **A full stop with a space in front of it is dirt** (`:479`), and this one is
     every language, because it is typography rather than English: punctuation
     attaches to the word before it, so a `.` that follows a space is a speck
     sitting in a word gap — the same dirt as the `_` below, read as a different
     mark. Fires on **6 cues in 1336**: `I .chose`, `[som de .1ixo`,
     `recebendo .ua` and the scratch-dot in `test_extracted 14` come out right,
     one is a garbage cue either way, and one (`taxonomia 112`) loses the real
     full stop off the end of an already-garbled line. Nothing that was right
     becomes wrong, and it fires on none of the 91 hand-written sidecar texts.
     - **A dot at the *start* of a line is left alone**, deliberately. There it is
       a degraded ellipsis, not dirt: `on_the_sea` opens three cues on a real `..`,
       and the one lone leading dot in the corpus has a sidecar reading
       `...if I agree with you`.
   - **English only** (`lang == "eng"`): the font makes `I`, `l`, `|` and a dotless
     `i` near-identical. Four of the subs are anchored by `(?<![^\s-])` — "preceded by
     whitespace, a hyphen, or start of string". The hyphen is there because a leading
     `-` marks the second speaker in a two-line cue and shouldn't detach the word.
     - Three run one way (`:485-487`): a bare `|` or `l` is the pronoun "I", as is
       either one carrying a contraction (`l'm`) or opening `If`.
     - One runs the other way (`:491`): a lowercase `i` that has lost its dot comes
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
   - **A capital in the middle of a sentence** is the same mark again, fused to the
     first letter of a word instead of a later one. The word before must end in a
     letter or a comma, which leaves alone anything opening a sentence, a
     `[speaker]` label, a quotation, or the `-` of a second speaker; and the word
     must be on `MID_SENTENCE_WORDS`. Fires on **9 cues in 1336** — 8 right, one
     on a cue that is garbage either way, none wrong: `mill, It was`,
     `Even If you`, `tangled In the barb wire` ×3 (confirmed by Vida Dentro's
     sidecar), `Oh, It's a balloon`, `No, It's fine here`, `I think It's him`,
     `the-camp Where people lived`, `This Is my …`.
     - **`MID_SENTENCE_WORDS` is an inclusion list, and that is the whole rule.**
       Testing instead whether the lower-case form is in `words.txt` was measured:
       it changes **79 cues and only 8 of them are errors**, because what a
       dictionary keeps is mostly proper nouns that are also ordinary words —
       `Heaven`, `Vale`, `God`, `Day`, `Lady`, `Christmas`, `Chile`, `Uruguay`,
       `Panama`, `Canon`, `Scope`, `Napoleon`, `French`. This is the `In` → `in`
       candidate from the sweep below, made safe by naming the words instead of
       asking a word list.
     - **`he`, `him`, `his`, `she` and `her` are deliberately off the list**, with
       the archaic second person. A capital there is how English writes a pronoun
       standing for God, and Ruvinho is full of that register. It costs two real
       fixes — `between His ribs`, `immortalized Him` — and they are a sidecar's
       job. Do not add them back to pick those up.
   - **A speck sitting above a letter is read as an accent** (`strip_speck_accent`,
     `:473`), which is the last of the marks-read-as-characters family. A
     lower-case token whose plain-ASCII form is in `words.txt` is dirt rather
     than a foreign word: `doés` → `does`, `thé` → `the`, `wé` → `we`. Fires
     on **4 lines in 1765** across the eleven films — 3 right, plus one already
     mangled `fragmentary` line (`néo` → `neo`) that is no worse either way.
     English only, like the rest of them.
     - **A token of a single letter is excluded, and that gate is the rule.**
       Without it `fragmentary`'s `é` → `e` fires three times and flattens a
       real Portuguese word. A lone accented letter is a word in Portuguese and
       never one in English, which is what keeps the bilingual film out.
     - **The word list is the other gate and is equally load-bearing.** Dropping
       it takes the sub to 9 lines and de-accents `perpétua` → `perpetua`,
       `especulacéo`, `abstracé&o` — correct Portuguese, flattened. This is the
       only place `clean` consults `words.txt`; see the note on it above.
     - **It was rejected once, on a measurement that found one instance, and
       `LINE_MERGE_RATIO` is what changed the count.** `test_extracted 34` reads
       `the` at the merged render scale and `thé` at the corrected one, so the
       clamp created its own customer. A rule that was below the bar can be put
       back over it by a change somewhere else in the pipeline.
   - **How thin the evidence has to be before a rule is worth adding.** The `ts`/`ls`
     sub was measured before it was written: it fires **3 times in ~1,240 cues** of
     raw OCR across the six English films, and all three are right. The three
     marks-read-as-characters subs were measured the same way and change
     **6 lines in 973** — 5 right, 1 garbage
     either way, none wrong. That is the bar; candidates found in the same sweep
     (`/ast` → `last`, bare `1` → `I`) were each correct too and were rejected for
     resting on one or two observations. Mid-sentence `In` → `in` came back later
     and is the capital rule above, once it stopped asking a dictionary. See the
     corrections analysis below.
   - **Three more that were measured and are not here**, so nobody re-derives them:
     - **A hyphen between two words is not removable.** The shape is real — the
       `-` of `type-of` and `not-see` is a dash-shaped speck sitting in a word gap,
       the same dirt as the `_`, and cue 44 of `test_extracted` carries one of each.
       But an inter-letter hyphen appears in **29 cues** and removing it is right in
       about 8 and wrong in about 21: `flip-flop` ×7, `space-time`, `T-shirt`,
       `quinta-feira`, `two-three`, and `close-up` in the very cue that motivated
       it. `test_extracted 84` settles it — `the-rain-does-no-know-how-to-fall.` is
       hyphenated on purpose, and the same line unhyphenated is cue 53. What
       separates a speck from a hyphen is the width of the gap it sits in, which is
       an image-step question; `clean` cannot ask it. Nor can the word list
       settle it: `words.txt` carries **no hyphenated entries at all**, so
       `flip-flop` and `not-see` look exactly alike to it. Measuring the gap
       means finding the hyphen's own connected component, which means the word
       box `read` already parses out of the TSV — scanning a whole band for
       flat, wide components catches every comma instead. Those 29 cues are the
       population to label if anyone tries.
     - **A leading apostrophe is the same mark again, and its rule was measured
       and not taken.** `^'` before a letter fires on **7 lines in 1765** — 6
       right and one garbage line. Four are `o_que` 69, 70, 75 and 82, where the
       drop-shadow face sheds a fragment off the crossbar of a `T`; two are
       `test_extracted` 46 and 72, where it is an ordinary speck. It needs an
       exception list to be safe, because `ora_esta 47` is a real `'Cause`
       (checked against the bitmap) — `(?i:cause|tis|til|em|bout|round|twas|neath)`
       is the draft. That is `CAMEL_WORDS`' shape and it works; it was left out
       because six cues are a sidecar's job and the list is one more thing to
       keep. Adopt it if a film turns up that opens lines on a mark.
     - **A full stop *between* two letters** is right on `officer.came` and
       `had.a` and wrong on `ocupaci.nal`, `moradon.s` and `gover.o`. The three
       losses are all Portuguese, where there is no word list to gate on.

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

### The merged band: two lines that `row_bands` sees as one

A band is a run of inked rows, so anything that puts ink in the gap between two
lines joins them into one band. Two things do: a mark crossing the gap — the
diagonal scratch in `test_extracted`'s cues 14 and 17 — and leading tight enough
that a descender meets the ascender below it, which is `o_que`'s whole second
half. **50 cues of 1336** are in this state.

When it happens, `line_h` comes out around 2.2× what a line really is, and
because every constant here is measured against `line_h`, three things go wrong
at once. On cue 17 of `test_extracted`:

| | merged (`line_h` 438) | corrected (194) |
|---|---|---|
| `render` scale, `TARGET_LINE_PX / line_h` | **0.073** | 0.165 |
| `GLYPH_RATIO * line_h`, the "this is a letter" test | 131 px → **17 of 56** components are letters | 58 px → 44 of 56 |
| `PUNCT_GAP * line_h`, how far a mark may sit and still be punctuation | **219 px** | 97 px |

So tesseract is handed 16 px a line, half of `TARGET_LINE_PX`, `despeckle` has
stopped recognising x-height letters as letters, and its reach for punctuation
has doubled. It costs whole words: `It has` comes back as `Ithas`.

**The fix is a second opinion, not a replacement.** `film_line_height` (`:227`)
takes the median over every band of every frame in the film, and `ocr` uses it
only when the frame's own measurement exceeds `LINE_MERGE_RATIO` times it. The
per-frame band stays in charge everywhere else, and it has to: a band is only as
tall as the letters that happen to be on its line, so over the eleven films a
true single line ranges from **0.48×** the film median (`acontece`, `euros`,
`some.` — no ascenders, no descenders) to **1.63×** (a line of bracketed, quoted
Portuguese). Swapping the film median in wholesale would rescale nearly every
cue and turn `TARGET_LINE_PX` into a differently-meaning constant.

**Why 1.7 and not lower.** Merged pairs run 1.67× to 2.49×, single lines top out
at 1.63×, and four hundredths is not a margin — do not read it as room to spare.
The two errors are not worth trading either: missing a merge leaves that cue
exactly as it was, while a false positive shrinks a correct one.

| clamp at | merges caught | fires on a cue that is not merged |
|---|---|---|
| 1.5× | 49/50 | 2 |
| 1.6× | 49/50 | 1 |
| **1.7×** | **45/50** | **0** |
| 1.8× | 39/50 | 0 |

Measured on the eleven films it fires on **44 cues, every one of which is a cue
where OCR found more lines than there were bands**, and on none of the other
1292. Of the 44: 29 change the SRT — **19 better, 6 worse, 4 mixed** — 6 more
change OCR that a `taxonomia` sidecar was already overriding, and 9 come out
identical. The wins are words (`Iwas` → `I was`, `Twas` → `I was`, `trom` →
`from`, `immediatdy` → `immediately`, `toa hospital` → `to a hospital`,
`didn rinteract` → `didn't interact`, `otigins` → `origins`); the losses are the
same kind of churn `deblob` produces (`into a trench` → `into atrench`, and
`test_extracted 14` turns `godfather.` into `godraines`). At the corrected scale
these cues are simply being read again, and tesseract moves both ways.

**Two other routes were tried and are worse.**

- **Splitting the band at a valley in the row-ink profile.** The signal is
  there — an inter-line gap dips to 3–4 % of the row peak while a single line's
  x-height shoulder bottoms out at 7–10 % — but at the best threshold found
  (5 % of peak, valley ≥ 4 % of the run) it changes 54 band counts, **45 toward
  the OCR's own line count and 9 wrongly**, mostly single lines cut in two.
- **A third render, arbitrated by confidence**, the way `close_gouges` is. It
  picks the wrong one: on cue 14 the merged render scores 84.0 against the
  corrected render's 82.3.

**Propagating the clamp into `despeckle` was tried and is worse.** `despeckle`
(`:265`) measures its own line height from the *un-clamped* bands, so on these
50 cues `GLYPH_RATIO` and `PUNCT_GAP` are both about 2.2× too large — which is
exactly why `test_extracted 34` keeps a speck out past the end of its first line
and reads `again, I`. Handing it the clamped height does remove that speck, so
the mechanism is real. It is still a loss: over the eleven films it changes
**3 cues and none of them for the better**. `o_que 102` collapses from
`In the-camp where people lived? tes` to `* In che wap where Pee Jived =`,
`taxonomia` gains a stray `à`, and cue 34 trades the speck for `again,` →
`agaln,` and `thé` → `tht`. A smaller `line_h` makes `despeckle` *more*
permissive about what counts as a letter and *less* about how far punctuation
may sit, and those two move in opposite directions. It also does nothing for
`test_extracted 17`, which carries the identical stray `I`. The specks these
cues carry are a sidecar's job.

**The cost, and it is a real one:** a cue's text is no longer a function of that
cue alone. The images are read twice — once cheaply for the median, then for
real — so running over half a film can move a cue that sits near
`LINE_MERGE_RATIO`. The pre-pass skips `despeckle` and `deblob`, which cost
about five times the rest and move the median not at all (389/389, 213/212,
194/194 on taxonomia, o_que, test_extracted), so it adds a few per cent:
0.011 s a cue on test_extracted, 0.115 s on taxonomia.

`want` is still the band count, so a merged cue still reports one band. The
`got > want` in step 5 is what identifies this population.

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

Three reports per cue, all on the text *after* corrections, all **purely
informational, never aborting**, and all naming the **cue number** rather than
the image, because a number is what somebody checking the result has in front of
them. The only fatal conditions are elsewhere: duplicate images (`:398`), no
images (`:495`), template mismatch (`:517`).

- `not text` → "cue N OCR'd to nothing".
- `got < want` → `cue 43 read 1 line(s) from 2 bands of ink`. One direction
  only; the section below is the whole argument for that, and it is worth
  reading before touching either half.
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

**The line-count check runs one way, and the reasoning matters** because both
halves are tempting and only one of them is sound. It compares the band count
from step 3.2 (`want`) with `len(text.splitlines())` (`got`).

A band is a run of inked rows, and no fully blank row can fall inside a single
line of text — so the band count can never *overcount* lines, only undercount
them, which it does whenever the leading is tight enough that a descender meets
the ascender below it and two real lines merge into one band. That made
`got > want` a false alarm every time: 44 of the 46 line-count warnings across
the eleven films were this direction, 38 in `o_que` alone, all on cues that were
transcribed perfectly.

**Those 44 were not noise, though, and it took until `LINE_MERGE_RATIO` to see
it.** `got > want` cannot be a statement about OCR quality, but given the
argument above it is an exact detector of something else: two lines sharing one
band, which silently halves that cue's render. It is the population the
line-height check is aimed at, and the reason it reads as harmless is only that
16 px a line is usually still legible. `got > want` stays the way to find these
cues; it just cannot *fix* them, because it is known only after the render it
would have corrected.

The other direction, `got < want`, is the one that is reported: a band of ink
came back with no line of text in it. It was dropped along with the rest when
both directions warned together, on the grounds that a couple of true positives
did not pay for the noise they arrived with — and it is back on its own now that
the noise is not attached to it. Measured over the eleven films it fires on
**2 cues of 1336, and both are real**: `test_extracted 43`, whose first line is
combed with dirt and reads as nothing, and `dizemos 199`, whose second line is
set in outline type. No false positives.

It is still not a *quality* check. "OCR'd to nothing" only fires on an empty
result, and this only on a line lost entirely, so **a cue that returns a little
bad text still passes silently** — that is what the suspect report and reading
the output are for.

### Step 6 — merge the cues the rip split (`merge_repeats`, `:839`)

The rips sometimes emit **one subtitle as several images**. The bitmap is
redrawn mid-display — the dirt on the scan moves, the caption is re-typeset, a
glyph drops out — and every redraw becomes its own file, its own filename, its
own cue, so the SRT carries the same line two or three times in a row. The
sidecars were already papering over this by hand: `taxonomia/corrections.json`
holds four *pairs* of consecutive entries reconstructed to the same text, which
is a person doing a merge's job one cue at a time.

A run of neighbours is folded while the next one reads the same and sits within
`MERGE_GAP_MS` of it. The run keeps the first cue's start and the last one's
end. Overlapping cues never fold (`gap < 0`), which is also what keeps a
template that is not in chronological order safe, and empty text never folds.

**The threshold is the whole argument.** Over the eleven films, 33 adjacent
pairs carry identical text:

| gap | count | what it is |
|---|---|---|
| 1 ms | 22 | the rippers' contiguous convention — a cue's end is the next one's start minus a millisecond, which is how they say the display never stopped |
| 10 ms | 1 | `test_extracted`'s convention; those two images are **byte-identical** |
| 67–301 ms | 7 | one to seven frames; every one was read against its bitmap and is the same caption drawn twice (`[Âncora] Bom dia!`, `Diane Arbus says:`, `Você!`) |
| 542 ms | 1 | `Vida Dentro` 107/108, where the second image is a half-drawn re-render and only the sidecar makes the two texts equal |
| 2671, 2837 ms | 2 | `on_the_sea` shows `Until the next sea arrives.` three times from an **identical bitmap, on purpose** |

The refrain at the bottom of that table is the population a threshold must
exclude, and **nothing in the corpus falls between 542 ms and 2671 ms** — so
500 ms is a threshold sitting in a hole two seconds wide, not a boundary
measured to the millisecond. Between 500 ms and `MERGE_REPORT_MS` identical text
is *reported* instead, which fires once, on that `Vida Dentro` pair.

Read against the general gap census the reading is plain: gaps cluster at
42/84/126/168/210/251/293 ms, which is one to seven frames at 24 fps, and 464 of
the 1335 boundaries are the 1 ms contiguity marker.

It absorbs **33 cues of 1336**, in seven of the eleven films: `Ruvinho` 101→99,
`dizemos` 255→252, `o_que` 121→116, `on_the_sea` 68→66, `onde` 104→103,
`taxonomia` 242→224, `test_extracted` 85→83.

**Corrections change the population, and that is not a bug to fix.** A sidecar
is what makes two degraded renders read alike: with sidecars `taxonomia` merges
18 cues and `onde` 1; on raw OCR — which is what the browser runs — `taxonomia`
merges 11 and `onde` none.

#### Near-identical neighbours (`reconcile`, `:744`)

Two renders of one subtitle usually do *not* read identically: the same line
through different dirt comes back a word or two apart. **17 contiguous pairs**
are 90 % alike without being equal, and they cannot be merged on similarity
alone, because four of them are genuinely different cues where a second
speaker's line appears between one and the next (`-If I?`, `- Aaaa...`,
`eu sei!`, `- Aham`).

What settles it is the word list. Where exactly one side of a differing pair is
in `words.txt`, that side is the real reading and wins; where both are (`tear`
and `teat`) or neither is (`Tupamaro` and `Tiipamaro`), the earlier render is
kept; and **if no differing word at all is in the list there is no evidence
here**, so the pair is reported rather than merged. In front of that sit the
gates that keep it off two cues that merely resemble each other: the texts must
be `MERGE_NEAR_RATIO` alike, must have the same shape line for line and word for
word (this is what excludes those four), must already agree on half their words,
and each differing pair must be within `MERGE_NEAR_EDITS` of its twin.

English only — it is the word list talking, and there is no Portuguese one.

It merges **three pairs in 1336 cues** and all three are right:

| | in | out |
|---|---|---|
| `o_que` 95/96 | `…in the camp…` + `ro tear down…`, then `…in the eamp…` + `to teat down…` | `…in the camp…` + `to tear down…` |
| `o_que` 100/101 | `…people lived.` and `…people lived,` | `…people lived.` |
| `test_extracted` 72/73 | `'That huge hall…` and `That huge hall…` | `That huge hall…` |

The first is the one worth reading twice: `camp` comes from the earlier render,
`to` from the later one, and `tear` is kept because `tear` and `teat` are both
English words. The merged text is therefore a line that appeared in **neither**
image, assembled from two readings of the same one.

**The known cost is the tie.** Where both words are real the earlier cue wins on
no evidence, so two genuinely different cues one word apart would fold and the
later word would go silently. Nothing in these films does that — the four that
would are excluded by the shape gate — but it is why the merge report prints the
word-level diff of everything `reconcile` changed. Read it.

#### What it reports and never touches

`repeat_notes` (`:804`) is the other half, in the manner of the suspect-word
report: identical text just past the merge window, and a near-identical
neighbour `reconcile` would not resolve. **15 lines over the eleven films** — 1
identical-but-542 ms-apart, 6 Portuguese (no word list), 7 whose line structure
differs, and `Tupamaro!` / `Tiipamaro!`, where neither reading is a word. They
are keyed to the cue numbers from *before* the merge, which is what step 5's
warnings and the `--dry-run` transcript use.

**A fuzzier rule was measured and is not here.** Merging on similarity alone
would fold those four second-speaker cues away; choosing between two full texts
rather than word by word would have to pick a loser; and requiring *every*
differing word to be decided — the first rule written — merges one pair in 1336
instead of three, because `tear`/`teat` and `lived.`/`lived,` are ties and a tie
is not a reason to keep two cues.

### Step 7 — write (`render_srt`, `:1120`)

`fill` (`:921`) with a template — keeps each block's number and timestamp, swaps the
`[sub_duration]` placeholder for the OCR'd body. `build` (`:958`) without — emits
`N / timestamp / text` numbered chronologically. Line endings match the template
(CRLF if it had them, CRLF by default when building fresh) because SRT players are
picky. `--dry-run` prints each transcription plus a speck count and returns early.

**A merge is the one thing that makes `fill` depart from its template**, and it
has to: the run's first block takes the merged range and the merged text, the
rest of the run's blocks are dropped, and the numbering — which the template
otherwise supplies — is rewritten 1..N so the file comes out without holes in
it. With nothing merged the template's numbers are kept untouched, so a film
that merges nothing is byte-for-byte what it was.

That renumbering is the one cost of the merge worth knowing about: a warning
printed during step 5 names the cue's number *before* anything folded, so on a
film that merges, a warning's number runs ahead of the output's. The merge
report is what reconciles them — it names the cues it folded, by those same
pre-merge numbers.

## `--jobs`

`transcribe(..., workers=N)` maps `ocr` over a `ThreadPoolExecutor` (`:798`). Safe:
`ocr` is a pure function of `(path, lang, film_line_h)` with its own
`TemporaryDirectory`, and no module state is mutated after startup. The same pool
runs the `film_line_height` pre-pass first; a median over every band is
order-independent, so that is deterministic too. `Executor.map` yields in *input*
order, so corrections and warnings still come out in strict cue order —
**`--jobs 4` must produce byte-identical output to `--jobs 1`, which is how to
test it**. It also sets
`OMP_THREAD_LIMIT=1` (`:797`), because tesseract 4.x uses OpenMP internally and
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
- **`DRIVER` is a JS template literal, so a backtick anywhere in that Python
  source closes the string and the whole worker stops parsing.** A module worker
  that fails to parse sends no message at all, so the page sat on "Downloading
  Python and the OCR engine…" for ever with nothing else to say — the runtime
  was never the problem, and every asset was being served correctly. It got in
  as Markdown quoting in a docstring (``` `transcribe` ```), which is how the
  rest of the project writes prose. Quote names bare inside `DRIVER`.
  **`cp worker-py.js /tmp/x.mjs && node --check /tmp/x.mjs` catches it**, and is
  worth running on both JS files before a push, because nothing else does.
- **A failure before `ready` is now visible.** `worker.onerror` and
  `onmessageerror` put the message in the report banner instead of letting the
  page hang silently, the download announces its three pieces as they start
  (Python, then the packages, then the pipeline) so a stalled stage can be
  named, and two minutes without `ready` prints the hard-refresh and
  unregister-the-service-worker advice.
- **The merge means the file holds fewer cues than the folder holds images**,
  so `do_run` returns both numbers as JSON rather than a bare warning count, and
  the finished banner says how many were merged. The bar still counts images,
  because that is what is being read. With no sidecar behind it the browser
  merges fewer cues than the command line — `taxonomia` 11 against 18.
- **The run has two passes and the bar shows both.** `film_line_height` reads
  every image before the first cue lands, so `transcribe` takes a `progress`
  callback; `do_run` passes `js_measure`, which posts a `measure` message, and
  the page shows "Measuring the type… n of N". The bar therefore fills once and
  restarts, which is honest — it is two passes. `clock.start` runs again when
  that pass ends, so the ETA is measured on the OCR alone and the first cue does
  not look like it took a minute. The command line passes no callback and prints
  nothing, so its stdout is unchanged.

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

## Tuning constants (`:48-162`)

`TARGET_LINE_PX 32`, `INK_THRESHOLD 128`, `MIN_BAND_RATIO 0.3`, `GLYPH_RATIO 0.3`,
`PUNCT_GAP 0.5`, `SHADOW_CLOSE_FRAC 0.06`. All are expressed relative to the measured
line height so they hold across films shot at different resolutions — keep it that way.

The others are not dials of the same kind. `BLOB_STROKES 4.4` and
`LINE_MERGE_RATIO 1.7` each sit on a measured boundary with its own section
above, and each has a known cost; read the section before changing either. So do
the four the merge uses — `MERGE_GAP_MS 500`, `MERGE_REPORT_MS 1000`,
`MERGE_NEAR_RATIO 0.9`, `MERGE_NEAR_EDITS 3` — except that the first of those
sits in a two-second hole in the data rather than on a boundary, and is the one
constant here that is measured in time rather than against the type.

## Known gotchas

- **`:486` is dead code.** `(?<![^\s-])[|l](?=')` never fires: the preceding sub's
  lookahead `(?![^\s'])` already admits a following apostrophe, so `l'm` is `I'm`
  before that line runs. Harmless, just redundant.
- **A bare pronoun followed by punctuation is not corrected** — `l.` stays `l.`,
  because `.` fails that same lookahead. Same for `l,` and `l?`. Consistent with the
  module's stated stance ("deliberately tiny"), but a real gap if a cue ends on `I`.
- `clean`'s fixes are English-only by design; don't extend them to `por` casually.

## Per-film data

Each film dir holds the JPEGs, optionally a template `.srt` (the ones containing
`[sub_duration]`) and optionally `corrections.json`. None of it is committed.

`cues` is what the SRT ends up holding: fewer than `imgs` wherever step 6 found
a subtitle the rip had split.

| dir | imgs | cues | template | corrections | lang |
|---|---|---|---|---|---|
| De Sol a Sol | 22 | 22 | yes | yes | eng |
| Ruvinho | 101 | 99 | yes | – | eng |
| Vida Dentro | 177 | 177 | – (built from filenames) | yes | eng |
| dizemos | 255 | 252 | yes | – | por |
| fragmentary | 68 | 68 | yes | yes | eng |
| o_que | 121 | 116 | yes | – | eng |
| on_the_sea | 68 | 66 | yes | – | por |
| onde | 104 | 103 | yes | yes | eng |
| ora_esta | 93 | 93 | yes | – | por |
| taxonomia | 242 | 224 | yes | yes | por |
| thanksgiving | 0 | – | – | – | source only |

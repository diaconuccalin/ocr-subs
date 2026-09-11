# subs/sol — bitmap subtitles → SRT

Turns a folder of subtitle *images* — bitmap subs ripped from a disc, one JPEG
per cue — into a timed `.srt`.

There are two ways to run it, and they are the same program:

- **In a browser**, at the published page. Nothing to install, and nothing is
  uploaded: the page runs this repository's own `ocr_subs.py` on your machine,
  inside the tab.
- **On the command line**, with `python3 ocr_subs.py`, which is what the films
  in this repository were transcribed with.

## The input contract

Every image filename encodes its cue's time range, and that is the join key for
the whole program:

```
0_04_29_960__0_04_33_719_2051018422813022638342156.jpeg
└ 0:04:29.960 ┘└ 0:04:33.719 ┘└ ignored id ┘
```

You can also supply a **template `.srt`** — one whose cue bodies are the literal
text `[sub_duration]`. It then decides the cue order and numbering, and the
image and template timestamps are checked against each other in *both*
directions: if either side has a cue the other lacks, the run stops rather than
risk putting a subtitle at the wrong time. Without a template, the cues are the
filenames in chronological order, numbered from 1.

## The page

Pick the folder of images in the first frame. If that folder also holds a
template, it is found and filled in for you. Choose a language, press the one
button, and you are asked where to save the result.

It needs a browser that can give a page `SharedArrayBuffer` — every current
Chrome, Edge, Firefox and Safari can. The page arranges the required headers
itself through a service worker and reloads once on a first visit to pick them
up, since GitHub Pages cannot set them. A real "where do you want to save this"
dialog is Chromium-only; elsewhere the file lands in your downloads folder.

The first visit downloads about 35 MB of runtime (Python, numpy, scipy, Pillow
and the tesseract engine, all compiled to WebAssembly) and the browser caches it.

### What the page does *not* do

It always runs raw OCR. The command line can additionally apply a
`corrections.json` sidecar — hand-written replacements for cues whose source
images are missing so many glyphs that no OCR could recover them — and the page
deliberately does not. For the films here that ship a sidecar, expect the page's
output to be visibly rougher than the committed `.srt`, and finish the job with
the command line.

### Two engines, one pipeline

Everything except the recognition step is the same code in both places: the
image preparation is numpy, scipy and Pillow either way, and it is exact — the
renders the browser produces are byte-identical to the ones the command line
produces.

The recognition differs. The command line runs whichever `tesseract` binary you
have installed; the page runs tesseract compiled to WebAssembly, which is a
newer release of the engine. On the films here that is worth **one to two per
cent of lines**, almost all of them in cues that are already badly degraded, and
it goes in both directions — some lines come out better, some worse. If you need
the two to agree exactly, use the command line for both.

## The command line

```sh
sudo apt install tesseract-ocr            # the binary, plus English
sudo apt install tesseract-ocr-por        # other languages are separate packages
pip install -r requirements.txt

python3 ocr_subs.py --dir "De Sol a Sol" --srt "2024 De Sol a Sol.srt"
python3 ocr_subs.py --dir "Vida Dentro"                 # no template
python3 ocr_subs.py --dir taxonomia --lang por --dry-run
```

`--srt` is resolved relative to `--dir`. Output defaults to
`<template>.filled.srt` with a template and `<dirname>.srt` without. Other
flags: `--out`, `--lang`, `--jobs N` (read N images at once; the output is
unchanged), `--corrections`, `--no-corrections`, `--dry-run`.

Language data that tesseract did not ship with can go in a `tessdata/`
directory beside the script, which saves needing root to install it
system-wide. If you do that, **`tessdata/configs` has to be there too** — a
symlink to your system one is fine. The script asks tesseract for its `tsv`
report by name, tesseract looks for that name under `$TESSDATA_PREFIX/configs/`,
and if it is missing tesseract writes a plain `.txt`, exits successfully, and
every image fails on a report that was never written. The script checks for this
at startup and says so.

## Publishing the page

GitHub Pages, deploying from a branch, `/` (root) — not `/docs`, so that the
page can serve the very same `ocr_subs.py` the command line runs, rather than a
copy that could drift from it. `.nojekyll` is committed because Pages otherwise
runs Jekyll and drops files whose names begin with an underscore.

## Layout

| | |
|---|---|
| `ocr_subs.py` | the whole pipeline, and the command line |
| `index.html`, `app.css`, `app.js` | the page |
| `worker-py.js` | runs `ocr_subs.py` under Pyodide, in a worker |
| `coi-serviceworker.js` | supplies the headers `SharedArrayBuffer` needs |
| `vendor/pyodide/` | Python, numpy, scipy, Pillow, as WebAssembly |
| `vendor/tesseract/` | the tesseract engine, as WebAssembly |
| `vendor/tessdata/` | language data for the page |
| `tessdata/` | language data for the command line (not committed) |

Third-party code under `vendor/` keeps its own licences.

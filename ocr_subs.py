#!/usr/bin/env python3
"""OCR extracted subtitle images into a timed SRT.

Each image filename encodes the cue's timestamp range:

    0_04_29_960__0_04_33_719_2051018422813022638342156.jpeg
    ^ 0:04:29.960  ^ 0:04:33.719  ^ ignored id

With `--srt`, an existing template SRT supplies the cue order and numbering and
this script replaces every `[sub_duration]` placeholder with the text OCR'd
from the image whose filename matches that range. Without it, the SRT is built
from the filenames alone, which is how a film with no template is handled.

    ./ocr_subs.py --dir "De Sol a Sol" --srt "2024 De Sol a Sol.srt"
    ./ocr_subs.py --dir "Vida Dentro"
"""

import argparse
import collections
import concurrent.futures
import difflib
import io
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

PLACEHOLDER = "[sub_duration]"

# 0_04_29_960__0_04_33_719_<id>.jpeg
NAME_RE = re.compile(
    r"^(\d+)_(\d\d)_(\d\d)_(\d\d\d)__(\d+)_(\d\d)_(\d\d)_(\d\d\d)_"
)

TIMESTAMP_RE = re.compile(
    r"^\d\d:\d\d:\d\d,\d\d\d --> \d\d:\d\d:\d\d,\d\d\d$"
)

# These images are enormous renders (8-11k px wide) of a handful of words, far
# past the size tesseract reads best. Each text line is scaled down to roughly
# this many pixels tall, measured on the ink itself rather than on the image,
# so the number holds across films shot at different resolutions.
TARGET_LINE_PX = 32

# Ink darker than this counts as text; the renders are black on white.
INK_THRESHOLD = 128

# A row band shorter than this fraction of the tallest one is speckle, not a
# line of text.
MIN_BAND_RATIO = 0.3

# Within a line, a component this much shorter than the line is punctuation or
# dirt rather than a letter.
GLYPH_RATIO = 0.3

# How close to the text, in line heights, a sub-letter mark has to sit before
# it counts as punctuation rather than dirt.
PUNCT_GAP = 0.5

# Confusions this font invites. Deliberately tiny: the input is clean, so
# aggressive "correction" would do more harm than good.
QUOTE_MAP = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})

# Some films set part of their cues in a drop-shadow display face: the glyph is
# drawn over an offset copy of itself, and where the two meet the render knocks
# a white gouge out of the stroke. Thresholding leaves those letters shredded
# and tesseract returns noise. Closing the mask by this fraction of the line
# height seals the gouges, which are far thinner than the counter of an `o`, so
# round letters survive. It is not free, though \u2014 on ordinary text the same
# closing thickens strokes enough to turn `0` into `8` \u2014 so both renders are
# read and the more confident one wins. See `read`.
SHADOW_CLOSE_FRAC = 0.06

# Language data tesseract did not ship with lives beside this script, since
# installing it system-wide needs root. Anything already in the environment
# wins, and the directory is simply absent for the films that only need `eng`.
LOCAL_TESSDATA = Path(__file__).resolve().parent / "tessdata"


class Abort(SystemExit):
    """A bad input rather than a bug.

    Subclassing SystemExit keeps the command line behaving exactly as it did —
    the interpreter prints the message and exits 1 — while a caller that is not
    a terminal, such as the browser build, can catch it and tell the difference
    between "your images don't line up" and a crash.
    """


def to_stderr(message):
    """Where the command line puts the warnings a caller might want instead."""
    print(message, file=sys.stderr)


# One record per cue, carrying everything a caller needs to report on it.
Cue = collections.namedtuple(
    "Cue", "stamp path text want got dropped changes warning"
)

# Everything decided before a single image is read: which cues exist, in what
# order, against which template, and what the sidecar says about them.
Plan = collections.namedtuple(
    "Plan", "by_timestamp stamps template newline default_out corrections"
)


def parse_timestamp(path):
    """Return the canonical SRT timestamp line encoded in a filename."""
    m = NAME_RE.match(path.name)
    if not m:
        return None
    h1, m1, s1, ms1, h2, m2, s2, ms2 = m.groups()
    return "{:02d}:{}:{},{} --> {:02d}:{}:{},{}".format(
        int(h1), m1, s1, ms1, int(h2), m2, s2, ms2
    )


def load_ink(path):
    """The image as a boolean mask of text pixels."""
    with Image.open(path) as im:
        return np.asarray(im.convert("L")) < INK_THRESHOLD


def row_bands(ink):
    """Row ranges holding text, one per rendered line, speckle rows dropped."""
    rows = ink.any(axis=1)
    runs, start = [], None
    for i, inked in enumerate(rows):
        if inked and start is None:
            start = i
        elif not inked and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(rows)))
    if not runs:
        return []
    tallest = max(b - a for a, b in runs)
    return [(a, b) for a, b in runs if b - a >= MIN_BAND_RATIO * tallest]


def despeckle(ink, bands):
    """Blank the JPEG dirt some frames carry in a corner, which tesseract
    otherwise reads as a stray word.

    The dirt is always a scatter of specks far smaller than a letter, so a
    line's letters fix the span that is really text, and anything below letter
    height sitting away from that span is thrown out. Note that a line's
    letters can be interrupted by a wide gap, because these renders drop
    glyphs: "was <hole> autiful?" is one line of text, not text plus dirt.
    """
    line_h = float(np.median([b - a for a, b in bands]))
    out = np.zeros_like(ink)
    dropped = 0
    for y0, y1 in bands:
        strip = ink[y0:y1]
        labels, count = ndimage.label(strip)
        if not count:
            continue
        boxes = ndimage.find_objects(labels)
        heights = np.array([b[0].stop - b[0].start for b in boxes])
        letters = np.nonzero(heights >= GLYPH_RATIO * line_h)[0]
        if not letters.size:
            continue
        x0 = min(boxes[i][1].start for i in letters)
        x1 = max(boxes[i][1].stop for i in letters)

        # Grow that span over adjacent punctuation. Repeated, because each dot
        # of an ellipsis only reaches the one before it.
        keep = set(int(i) for i in letters)
        rest = [i for i in range(count) if i not in keep]
        grew = True
        while grew:
            grew = False
            for i in list(rest):
                cx0, cx1 = boxes[i][1].start, boxes[i][1].stop
                if max(x0 - cx1, cx0 - x1, 0) <= PUNCT_GAP * line_h:
                    x0, x1 = min(x0, cx0), max(x1, cx1)
                    rest.remove(i)
                    keep.add(i)
                    grew = True
        dropped += len(rest)

        mask = np.zeros(count + 1, bool)
        for i in keep:
            mask[i + 1] = True
        out[y0:y1] = mask[labels]
    return out, dropped


def render(ink, line_h):
    """The ink mask as a PNG, downscaled to the size tesseract reads best."""
    im = Image.fromarray(np.where(ink, 0, 255).astype(np.uint8))
    width, height = im.size
    scale = min(1.0, TARGET_LINE_PX / line_h)
    if scale < 1.0:
        im = im.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def close_gouges(ink, line_h):
    """Seal the white gouges the drop-shadow face cuts into its own strokes.

    A disk of the right radius costs seconds per image at these dimensions, so
    the closing is iterated over a 3x3 instead: same result to within a percent
    of pixels, about thirty times quicker.
    """
    r = max(1, int(round(SHADOW_CLOSE_FRAC * line_h)))
    box = np.ones((3, 3), bool)
    grown = ndimage.binary_dilation(ink, structure=box, iterations=r)
    return ndimage.binary_erosion(grown, structure=box, iterations=r)


def run_tesseract(png, lang, workdir):
    """The TSV report the local `tesseract` binary gives for one render."""
    base = str(Path(workdir) / "page")
    # --psm 6 ("uniform block of text") is the mode that keeps line breaks,
    # which is what makes the two-line cues come back as two lines.
    proc = subprocess.run(
        ["tesseract", "-", base, "-l", lang, "--psm", "6", "tsv"],
        input=png,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "tesseract failed: {}".format(
                proc.stderr.decode("utf-8", "replace").strip()
            )
        )
    return Path(base + ".tsv").read_text(encoding="utf-8")


# Where `read` gets its TSV from. WebAssembly has no subprocesses, so the
# browser build rebinds this to a call into tesseract.js; nothing else in the
# pipeline can tell the difference.
TESSERACT = run_tesseract


def read(png, lang, workdir):
    """Tesseract's transcription of one render, with its own confidence in it.

    Reads the TSV report rather than plain text, because the per-word
    confidences are what let the caller choose between two renders of the same
    frame. Line breaks survive: the report numbers the lines.
    """
    confidences, lines, word, line_id = [], [], [], None
    for row in TESSERACT(png, lang, workdir).splitlines()[1:]:
        f = row.split("\t")
        # Level 5 is a word; the coarser levels repeat its box and carry no text.
        if len(f) < 12 or f[0] != "5" or not f[11].strip():
            continue
        confidences.append(float(f[10]))
        here = (f[2], f[3], f[4])  # block, paragraph, line
        if here != line_id and word:
            lines.append(" ".join(word))
            word = []
        line_id = here
        word.append(f[11])
    if word:
        lines.append(" ".join(word))
    mean = sum(confidences) / len(confidences) if confidences else 0.0
    return clean("\n".join(lines), lang), mean


def ocr(path, lang="eng"):
    """Transcribe one frame, reading it both plain and de-gouged.

    Neither render wins everywhere — closing rescues the drop-shadow face and
    corrupts ordinary digits — so both are read and tesseract's confidence
    picks. Ties go to the plain render, which is right far more often.
    """
    ink = load_ink(path)
    bands = row_bands(ink)
    if not bands:
        return "", 0, 0
    ink, dropped = despeckle(ink, bands)
    bands = row_bands(ink) or bands
    line_h = float(np.median([b - a for a, b in bands]))

    with tempfile.TemporaryDirectory() as workdir:
        plain, plain_conf = read(render(ink, line_h), lang, workdir)
        sealed, sealed_conf = read(
            render(close_gouges(ink, line_h), line_h), lang, workdir
        )
    text = sealed if sealed_conf > plain_conf else plain
    return text, len(bands), dropped


def clean(raw, lang="eng"):
    """Normalize whitespace and fix the few glyph confusions worth fixing."""
    lines = []
    for line in raw.translate(QUOTE_MAP).splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        # A bare `|` or `l` is the pronoun "I", as is either one carrying a
        # contraction (`l'm`, `l've`) or opening `If`. A leading `-` marks the
        # second speaker in a two-line cue, so it doesn't break the word.
        # English only: in other languages these shapes are real words.
        if lang == "eng":
            line = re.sub(r"(?<![^\s-])[|l](?![^\s'])", "I", line)
            line = re.sub(r"(?<![^\s-])[|l](?=')", "I", line)
            line = re.sub(r"(?<![^\s-])[|l]f\b", "If", line)
        lines.append(line)
    return "\n".join(lines)


def load_corrections(path):
    """Read the timestamp -> reconstructed-text sidecar, if present."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def word_diff(before, after):
    """Word-level changes between OCR output and its correction."""
    a, b = before.split(), after.split()
    changes = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            continue
        changes.append((" ".join(a[i1:i2]) or "∅", " ".join(b[j1:j2]) or "∅"))
    return changes


def collect(image_dir, report=to_stderr):
    """Map each timestamp line to its image, failing loudly on ambiguity."""
    by_timestamp = {}
    unparsed = []
    for path in sorted(image_dir.glob("*.jpeg")) + sorted(image_dir.glob("*.jpg")):
        stamp = parse_timestamp(path)
        if stamp is None:
            unparsed.append(path.name)
            continue
        if stamp in by_timestamp:
            raise Abort(
                "two images claim {}: {} and {}".format(
                    stamp, by_timestamp[stamp].name, path.name
                )
            )
        by_timestamp[stamp] = path
    if unparsed:
        report(
            "warning: ignoring {} file(s) with unrecognized names: {}".format(
                len(unparsed), ", ".join(unparsed)
            )
        )
    return by_timestamp


def srt_timestamps(text):
    """The timestamp line of every block in the template, in order."""
    stamps = []
    for block in text.strip().split("\n\n"):
        for line in block.splitlines():
            if TIMESTAMP_RE.match(line.strip()):
                stamps.append(line.strip())
                break
    return stamps


def fill(text, texts):
    """Replace each block's placeholder with the OCR'd text for its timestamp."""
    out_blocks = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        stamp = next(
            (l.strip() for l in lines if TIMESTAMP_RE.match(l.strip())), None
        )
        if stamp is None:
            out_blocks.append(block)
            continue
        body = texts[stamp]
        head = lines[: lines.index(next(l for l in lines if l.strip() == stamp)) + 1]
        rest = [l for l in lines[len(head):] if l.strip() != PLACEHOLDER]
        out_blocks.append("\n".join(head + [body] + rest))
    return "\n\n".join(out_blocks) + "\n"


def build(stamps, texts):
    """Write an SRT from scratch, numbering the cues in chronological order."""
    blocks = [
        "{}\n{}\n{}".format(i, stamp, texts[stamp])
        for i, stamp in enumerate(stamps, 1)
    ]
    return "\n\n".join(blocks) + "\n"


def sort_key(stamp):
    """Chronological order for a timestamp line."""
    h, m, rest = stamp[:8].split(":")
    return (int(h), int(m), int(rest), int(stamp[9:12]))


def setup_tessdata():
    """Point tesseract at the language data beside this script, if it needs it.

    Anything already in the environment wins, and the directory is simply
    absent for the films that only need `eng`. The `configs` check is there
    because `read` asks tesseract for its `tsv` config, which tesseract
    resolves as `$TESSDATA_PREFIX/configs/tsv`: without that file it writes a
    plain `.txt` and exits 0, and the only symptom is `read` failing on a
    missing `.tsv` for every single image.
    """
    if "TESSDATA_PREFIX" not in os.environ and LOCAL_TESSDATA.is_dir():
        os.environ["TESSDATA_PREFIX"] = str(LOCAL_TESSDATA)
    prefix = os.environ.get("TESSDATA_PREFIX")
    if not prefix:
        return
    # Tesseract has meant both "the tessdata directory" and "its parent" by
    # this variable over the years; accept either rather than cry wolf.
    root = Path(prefix)
    if any((d / "configs" / "tsv").exists() for d in (root, root / "tessdata")):
        return
    to_stderr(
        "warning: no configs/tsv under {}, so tesseract cannot write the TSV "
        "report this script reads. Copy or symlink `configs` from your system "
        "tessdata directory.".format(prefix)
    )


def plan(image_dir, srt=None, corrections_name="corrections.json",
         no_corrections=False, report=to_stderr):
    """Decide which cues exist and in what order, before any image is read.

    With a template the template supplies the order and the numbering, and the
    timestamps are checked in both directions so a mis-parsed filename can
    never silently place a subtitle at the wrong time. Without one the cue list
    is the filenames, in chronological order.
    """
    by_timestamp = collect(image_dir, report)
    if not by_timestamp:
        raise Abort("no subtitle images found in {}".format(image_dir))

    template = None
    if srt:
        srt_path = Path(srt)
        if not srt_path.is_absolute():
            srt_path = image_dir / srt_path
        raw = srt_path.read_bytes()
        # Match the template's line endings; SRT players are picky and the
        # existing file is CRLF.
        newline = "\r\n" if b"\r\n" in raw else "\n"
        template = raw.decode("utf-8").replace("\r\n", "\n")
        stamps = srt_timestamps(template)
        default_out = srt_path.with_suffix(".filled" + srt_path.suffix)

        missing = [s for s in stamps if s not in by_timestamp]
        extra = [s for s in by_timestamp if s not in stamps]
        if missing or extra:
            for s in missing:
                report("no image for SRT entry {}".format(s))
            for s in extra:
                report("no SRT entry for image {}".format(by_timestamp[s].name))
            raise Abort("aborting: image/SRT timestamps do not line up")
    else:
        newline = "\r\n"
        stamps = sorted(by_timestamp, key=sort_key)
        name = image_dir.resolve().name
        default_out = image_dir / "{}.srt".format(name)

    corrections = (
        {} if no_corrections else load_corrections(image_dir / corrections_name)
    )
    unused = [k for k in corrections if k not in by_timestamp]
    if unused:
        report(
            "warning: {} correction(s) match no cue: {}".format(
                len(unused), ", ".join(unused)
            )
        )
    return Plan(by_timestamp, stamps, template, newline, default_out, corrections)


def transcribe(stamps, by_timestamp, corrections, lang="eng", workers=1):
    """Steps 3 to 5 for every cue, yielding one Cue each, in cue order.

    `workers` only changes how many images are in flight: `ocr` is a pure
    function of its arguments and builds its own temporary directory, and
    Executor.map yields in *input* order, so the corrections and the warnings
    still come out in strict cue order and the output is unchanged.
    """
    paths = [by_timestamp[s] for s in stamps]
    pool = None
    try:
        if workers > 1:
            # Tesseract uses OpenMP internally; several multithreaded copies of
            # it would fight over the same cores for a net loss.
            os.environ.setdefault("OMP_THREAD_LIMIT", "1")
            pool = concurrent.futures.ThreadPoolExecutor(workers)
            results = pool.map(ocr, paths, itertools.repeat(lang))
        else:
            results = (ocr(path, lang) for path in paths)

        for stamp, path, (text, want, dropped) in zip(stamps, paths, results):
            changes = None
            if stamp in corrections and corrections[stamp] != text:
                changes = word_diff(text, corrections[stamp])
                text = corrections[stamp]
            got = len(text.splitlines())
            if not text:
                warning = "warning: {} OCR'd to nothing".format(path.name)
            elif got != want:
                warning = "warning: {} looks like {} line(s) but OCR'd to {}".format(
                    path.name, want, got
                )
            else:
                warning = None
            yield Cue(stamp, path, text, want, got, dropped, changes, warning)
    finally:
        if pool is not None:
            pool.shutdown(wait=False)


def render_srt(template, stamps, texts, newline):
    """The finished SRT as bytes, with the line endings the caller asked for."""
    body = fill(template, texts) if template else build(stamps, texts)
    return body.replace("\n", newline).encode("utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default=".", help="directory holding the images")
    ap.add_argument(
        "--srt",
        default=None,
        help="template SRT; omit to build the SRT from the image filenames",
    )
    ap.add_argument("--out", default=None, help="output SRT")
    ap.add_argument(
        "--lang",
        default="eng",
        help="tesseract language, e.g. por for Portuguese (default: eng)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print transcriptions, write nothing")
    ap.add_argument(
        "--corrections",
        default="corrections.json",
        help="sidecar of hand-reconstructed cues, read from --dir (default: corrections.json)",
    )
    ap.add_argument(
        "--no-corrections",
        action="store_true",
        help="use raw OCR only, ignoring the corrections sidecar",
    )
    ap.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="read this many images at once; output is unchanged (default: 1)",
    )
    args = ap.parse_args()

    setup_tessdata()

    p = plan(
        Path(args.dir), args.srt, args.corrections, args.no_corrections
    )
    out_path = Path(args.out) if args.out else p.default_out

    texts = {}
    applied = []
    warnings = 0
    for cue in transcribe(
        p.stamps, p.by_timestamp, p.corrections, args.lang, args.jobs
    ):
        texts[cue.stamp] = cue.text
        if cue.changes is not None:
            applied.append((cue.stamp, cue.changes))
        if cue.warning:
            print(cue.warning, file=sys.stderr)
            warnings += 1
        if args.dry_run:
            print("{}{}\n{}\n".format(
                cue.stamp,
                "  [{} speck removed]".format(cue.dropped) if cue.dropped else "",
                cue.text,
            ))

    if applied:
        print("\ncorrections applied ({} cue(s)):".format(len(applied)), file=sys.stderr)
        for stamp, changes in applied:
            for was, now in changes:
                print("  {}  {!r} -> {!r}".format(stamp[:12], was, now), file=sys.stderr)

    if args.dry_run:
        print(
            "\n{} image(s), {} warning(s); nothing written".format(len(texts), warnings),
            file=sys.stderr,
        )
        return

    out_path.write_bytes(render_srt(p.template, p.stamps, texts, p.newline))
    print(
        "wrote {} ({} cues, {} warning(s))".format(out_path, len(texts), warnings)
    )


if __name__ == "__main__":
    main()

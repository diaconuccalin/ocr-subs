#!/usr/bin/env python3
"""OCR extracted subtitle images into a timed SRT.

Each image filename encodes the cue's timestamp range:

    0_04_29_960__0_04_33_719_2051018422813022638342156.jpeg
    ^ 0:04:29.960  ^ 0:04:33.719  ^ ignored id

With `--srt`, an existing template SRT supplies the cue order and numbering and
this script replaces every `[sub_duration]` placeholder with the text OCR'd
from the image whose filename matches that range. Without it, the SRT is built
from the filenames alone, which is how a film with no template is handled.

Where the rip split one subtitle across several images — the bitmap redrawn
mid-display, so the same line arrives two or three times in a row — those cues
are folded back into one and the output is renumbered; `--no-merge` keeps them
apart.

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
import unicodedata
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

# What may legitimately follow an apostrophe inside an English word: the
# contractions, the possessive `s`, and the few elisions that turn up in
# speech. Everything on this list was found in the films except the last four,
# which are here because they are ordinary English and the list only ever
# prevents a change.
APOSTROPHE_TAILS = r"(?:s|t|m|d|re|ve|ll|am|all|n|clock|em|til|cause)"

# Words that carry a capital in the middle on purpose and start lower-case, so
# the rule below cannot tell them from a mark read as a letter. Short on
# purpose: this is a list of exceptions, not a dictionary.
CAMEL_WORDS = frozenset(
    ["ebay", "ebook", "imac", "ios", "ipad", "iphone", "ipod", "itunes"]
)

# Words that may lose their capital when one turns up in the middle of a
# sentence, where a mark fused to the first letter is the likeliest
# explanation: `mill, It was`, `Even If you`, `tangled In the barb wire`.
#
# An inclusion list rather than a dictionary, and for the usual reason. Testing
# instead whether the lower-case form is in `words.txt` was measured: it
# changes 79 cues across the six English films and only 8 of them are errors,
# because the rest are proper nouns that are also ordinary words — `Heaven`,
# `Vale`, `God`, `Day`, `Lady`, `Chile`, `Panama`, `Canon`, `Scope`,
# `Napoleon`, `French`. Kept to function words, it changes 9 cues of 1336: 8 of
# them right, one on a cue that is garbage either way, none wrong.
#
# `he`, `him`, `his`, `she` and `her` are deliberately absent, along with the
# archaic second person: a capital on those is how English writes a pronoun
# standing for God, and Ruvinho is full of that register. It costs two real
# fixes (`between His ribs`, `immortalized Him`) and they are a sidecar's job.
MID_SENTENCE_WORDS = frozenset("""
    a an the this that these those there then and but or nor so as if when
    where what who how why while because although though than
    of to for with without from by at in into on onto up out over under about
    it its they them we you my your our their
    is are was were be been being am do does did have has had
    not no yes very just only also still even more most much many some any
""".split())

# Some films set part of their cues in a drop-shadow display face: the glyph is
# drawn over an offset copy of itself, and where the two meet the render knocks
# a white gouge out of the stroke. Thresholding leaves those letters shredded
# and tesseract returns noise. Closing the mask by this fraction of the line
# height seals the gouges, which are far thinner than the counter of an `o`, so
# round letters survive. It is not free, though \u2014 on ordinary text the same
# closing thickens strokes enough to turn `0` into `8` \u2014 so both renders are
# read and the more confident one wins. See `read`.
SHADOW_CLOSE_FRAC = 0.06

# Some rips carry solid blots lying on the words — a filled-in counter, a blob
# of spatter, a scratch — big enough and close enough that `despeckle` keeps
# them and tesseract reads them as letters: a wedge over "mud" comes back as
# "Mud". A mark this many times fatter than the typical ink in its frame is not
# type, and gets dropped whole.
#
# This one is a judgement call rather than a clean separation, and the reasoning
# is under "The dirt `despeckle` does not catch" in CLAUDE.md. Measured over the
# eleven films it changes 34 cues of 1251: 16 come out better, 7 worse, 1 mixed.
# The losses are real and they are the quiet kind — `48ºC` read as `458ºC` —
# so read the output rather than trusting it.
BLOB_STROKES = 4.4

# A band is a run of inked rows, so a mark crossing the gap between two lines
# joins them into one: `row_bands` returns a single band twice the height of a
# line, and every constant here is measured against that height. The render is
# then downscaled to half the size tesseract reads best, and `despeckle` stops
# recognising x-height letters as letters. It costs real words — `It has` comes
# back as `Ithas` — on 50 cues of 1336.
#
# A band this many times taller than the film's typical line cannot be one line
# of type, so `ocr` falls back to the film's own measurement. Set high on
# purpose: missing a merge leaves that cue exactly as it was, while a false
# positive shrinks a correct one, so the two errors are not worth trading. At
# 1.7x this catches 45 of the 50 and changes none of the other 1286. The
# highest ratio a single line of type reaches over the eleven films is 1.63,
# a line of bracketed, quoted text in taxonomia; the lowest a merged pair
# reaches is 1.67, so do not read the gap between them as room to spare.
LINE_MERGE_RATIO = 1.7

# Italic type leans forward, and nothing else in a subtitle bitmap does. The
# slant is measured by deslanting: shear the ink by a candidate amount, take the
# column-ink profile, and the shear that stands the vertical stems upright is
# the one whose profile is most concentrated. `shear_slant` searches this range
# at this step and returns the shear that wins, negative for a forward lean.
#
# The range is wider than any face needs so that a bad measurement lands outside
# the gates below rather than being clamped into them.
SHEAR_RANGE = 0.40
SHEAR_STEP = 0.02

# How far a band has to lean before it is italic rather than noise. Measured on
# all 1716 bands of the eleven films: upright bands sit at exactly 0.00 and no
# band of the five upright films reaches -0.08, while the italic populations
# cluster per film at -0.16 (dizemos), -0.18 (taxonomia), -0.20 (on_the_sea,
# ora_esta, test_extracted) and -0.34 (fragmentary). This sits in the middle of
# a gap that is wide, unlike the one `LINE_MERGE_RATIO` has to live in.
ITALIC_SHEAR = 0.10

# A slant needs type to measure. Every false positive in the corpus is a band
# carrying one or two words — `- Yes` and `Yes.` at +0.34, `Why?` at -0.30 —
# and note the last one leans the right way, so requiring a forward lean is not
# enough on its own. Those three span 2.6 to 3.2 line heights. A band narrower
# than this is not measured and takes no verdict of its own.
ITALIC_BAND_WIDTH = 4.0

# The same gate for one word, which is necessarily narrower. Without it the word
# rule fires 51 times across the corpus on marks that have no slant to measure:
# 48 lone second-speaker hyphens in dizemos and 3 punctuation fragments in
# taxonomia, all of them 0.0 to 0.3 line heights wide. The narrowest word that
# is really italic is `eu`, at 1.3.
ITALIC_WORD_WIDTH = 1.0

# A word is too small a sample for the absolute test above, so it is matched
# against the film's own italic angle instead, within this much. That is also
# what makes the word rule safe: a film with no italic bands has no angle, so
# the rule never runs there at all and the upright films cannot be touched by it.
ITALIC_TOL = 0.04

# Two runs of ink closer than this many line heights are one word. Only used to
# split a band into words, so that a single italic word inside an upright line
# can be found.
ITALIC_WORD_GAP = 0.22

# `shear_slant` costs 0.25s a cue on taxonomia if it reads every ink pixel,
# which is what `deblob` costs and far too much for a measurement this small.
# Striding the ink down to this many pixels takes it to 0.022s and changes no
# verdict. Strided rather than sampled, so it stays a pure function of the mask
# and `--jobs 4` still matches `--jobs 1`.
ITALIC_SAMPLE = 40000

# Below this many ink pixels there is nothing to measure at all.
ITALIC_MIN_INK = 30

# Language data tesseract did not ship with lives beside this script, since
# installing it system-wide needs root. Anything already in the environment
# wins, and the directory is simply absent for the films that only need `eng`.
LOCAL_TESSDATA = Path(__file__).resolve().parent / "tessdata"

# A word list, beside this script, for the suspect-word report. Absent is fine:
# the report is simply skipped.
LOCAL_WORDS = Path(__file__).resolve().parent / "words.txt"

# Words shorter than this have too many neighbours for "one edit away" to mean
# anything, so they are never reported.
SUSPECT_LENGTH = 4

# The rips sometimes emit one subtitle as several images: the bitmap is redrawn
# — the dirt on the scan moves, the caption is re-typeset, a glyph drops out —
# and every redraw becomes its own file, its own cue, its own repeat of the same
# line. Two cues carrying the same text this close together are that, and are
# folded back into one. Measured over the eleven films: of the 33 adjacent pairs
# that carry identical text, 22 sit 1 ms apart (the rippers write the end of a
# cue as the next one's start minus a millisecond, which is how they say "the
# display never stopped"), one sits 10 ms apart with byte-identical images, and
# seven more sit 67 to 301 ms apart — one to seven frames — every one of which
# was read against its bitmap and is the same caption drawn twice.
#
# The population this must not touch is at the other end: `on_the_sea` shows
# `Until the next sea arrives.` three times from an identical bitmap, 2.7 s
# apart, on purpose. Nothing in the corpus falls between 542 ms and 2671 ms,
# which is the room this threshold sits in rather than a boundary measured to
# the millisecond.
MERGE_GAP_MS = 500

# Identical text further apart than that is reported and left alone, up to here.
# It fires once in 1336 cues, on `Vida Dentro` 107/108 at 542 ms, where the
# second image is a half-drawn re-render and only its sidecar makes the two
# texts equal. Past this, a repeat is assumed to be a repeat.
MERGE_REPORT_MS = 1000

# A neighbour need not read identically to be the same caption: the same line
# OCR'd twice through different dirt comes back a word or two apart. These two
# say how far apart the texts, and each differing word inside them, may be
# before they are no longer plausibly the same line. See `reconcile`, which is
# the only thing that can act on it, and only in English.
MERGE_NEAR_RATIO = 0.9
MERGE_NEAR_EDITS = 3


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
    "Cue",
    "number stamp path text want got dropped blobs marked changes warning suspects",
)

# Everything decided before a single image is read: which cues exist, in what
# order, against which template, and what the sidecar says about them.
Plan = collections.namedtuple(
    "Plan", "by_timestamp stamps template newline default_out corrections"
)

# One record per run of cues folded into one: the timestamp line the run now
# carries, the original stamps in order, the cue number the run started at, how
# it was decided (`identical` or `reconciled`) and, when reconciling changed a
# word, what it changed.
Merge = collections.namedtuple("Merge", "stamp absorbed number kind changes")


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


def shear_slant(mask):
    """How far the type in this mask leans, as a shear, or None if too little ink.

    Deslanting: move every ink pixel to `c + s * (mean_row - r)` for each
    candidate shear `s`, take the column-ink profile, and score it by its energy
    `sum(p**2) / n**2`. Vertical stems are what a text line has most of, so the
    profile is most concentrated at the shear that stands them upright, and the
    shear that wins is the slant. Upright type returns 0.0 exactly; italic leans
    forward, which in this convention is negative.

    The energy is divided by the pixel count squared so the score does not
    depend on how much ink there is, which is what lets one word be compared
    against a whole line.
    """
    rows, cols = np.nonzero(mask)
    if len(rows) < ITALIC_MIN_INK:
        return None
    if len(rows) > ITALIC_SAMPLE:
        # Strided, not random: this has to stay a pure function of the mask, or
        # `--jobs 4` would stop matching `--jobs 1`.
        step = len(rows) // ITALIC_SAMPLE + 1
        rows, cols = rows[::step], cols[::step]
    lift = (rows.mean() - rows).astype(np.float64)
    scale = float(len(rows)) ** 2
    best, at = -1.0, 0.0
    steps = int(round(SHEAR_RANGE / SHEAR_STEP))
    for i in range(-steps, steps + 1):
        s = i * SHEAR_STEP
        moved = np.rint(cols + s * lift).astype(np.int64)
        profile = np.bincount(moved - moved.min()).astype(np.float64)
        score = float((profile * profile).sum()) / scale
        if score > best:
            best, at = score, s
    return at


def ink_width(mask):
    """How many columns lie between the first and last inked one."""
    cols = np.nonzero(mask.any(axis=0))[0]
    return 0 if not len(cols) else int(cols[-1] - cols[0] + 1)


def word_runs(band, gap):
    """Column spans of one word each, runs of ink closer than `gap` joined."""
    inked = band.any(axis=0)
    runs, start = [], None
    for i, on in enumerate(inked):
        if on and start is None:
            start = i
        elif not on and start is not None:
            runs.append([start, i])
            start = None
    if start is not None:
        runs.append([start, len(inked)])
    joined = []
    for run in runs:
        if joined and run[0] - joined[-1][1] < gap:
            joined[-1][1] = run[1]
        else:
            joined.append(run)
    return [(a, b) for a, b in joined]


def band_slants(ink, bands, line_h):
    """Each band's slant and how wide it is, in line heights.

    The pair the italic rules are written against: a slant on its own says
    nothing until you know there was enough type to measure it on.
    """
    out = []
    for a, b in bands:
        band = ink[a:b]
        width = ink_width(band) / line_h if line_h else 0.0
        out.append((shear_slant(band), width))
    return out


def is_italic_band(slant, width):
    """Whether a band's own measurement says italic, on the absolute test."""
    return (
        slant is not None
        and slant <= -ITALIC_SHEAR
        and width >= ITALIC_BAND_WIDTH
    )


def band_italics(ink, bands, line_h, film_slant):
    """One verdict per band, plus one per word inside it.

    The band verdict is the absolute test above. The word verdicts are the
    relative one: a run is italic when it leans within `ITALIC_TOL` of the
    film's own italic angle, upright when it leans within the same of nothing,
    and None — no evidence, take the band's word for it — otherwise. A run
    narrower than `ITALIC_WORD_WIDTH` is never measured, because a hyphen or a
    comma has no stems and its slant is noise.

    `film_slant` of None means the film showed no italic bands at all, and the
    word rule is then switched off entirely rather than being pointed at a
    guess.
    """
    verdicts = []
    for (a, b), (slant, width) in zip(bands, band_slants(ink, bands, line_h)):
        italic = is_italic_band(slant, width)
        runs = []
        for lo, hi in word_runs(ink[a:b], ITALIC_WORD_GAP * line_h):
            here = None
            if film_slant is not None and (hi - lo) / line_h >= ITALIC_WORD_WIDTH:
                measured = shear_slant(ink[a:b, lo:hi])
                if measured is not None:
                    if abs(measured - film_slant) <= ITALIC_TOL:
                        here = True
                    elif abs(measured) <= ITALIC_TOL:
                        here = False
            runs.append(here)
        verdicts.append((italic, runs))
    return verdicts


# A closing tag should not swallow the full stop after the word it emphasises,
# so a span ends at the last letter or digit in it.
TAIL_PUNCTUATION = re.compile(r"[^0-9A-Za-z\u00c0-\u024f]+$")


def mark_italics(text, verdicts):
    """Wrap the italic parts of one cue's text in `<i>`.

    Lines pair with bands by position, and words with word runs by position.
    Neither pairing is guaranteed — a band holding two lines of type reports one
    band for two lines of text, which is the `got > want` population — so each
    is checked and falls back rather than being trusted: a line count that does
    not match the band count drops to "wrap the cue only if every band of it is
    italic", and a word count that does not match the run count lets the band's
    own verdict cover the whole line.

    A cue that is italic throughout is wrapped once, opening before its first
    line and closing after its last, which is how the tag is usually written.
    """
    if not text or not verdicts:
        return text
    lines = text.split("\n")
    if all(italic for italic, _ in verdicts):
        return "<i>" + text + "</i>"
    if len(lines) != len(verdicts):
        return text
    out = []
    for line, (italic, runs) in zip(lines, verdicts):
        words = line.split()
        if not words:
            out.append(line)
            continue
        if len(words) == len(runs):
            flags = [italic if run is None else run for run in runs]
        else:
            flags = [italic] * len(words)
        out.append(" ".join(_tag_words(words, flags)))
    return "\n".join(out)


def _tag_words(words, flags):
    """The words of one line with `<i>` opened and closed around each run."""
    tagged = list(words)
    start = None
    for i in range(len(words) + 1):
        on = i < len(words) and flags[i]
        if on and start is None:
            start = i
        elif not on and start is not None:
            last = i - 1
            # A span covering the whole line keeps its full stop, the way a
            # whole italic cue does; a span covering part of one does not, so
            # `the <i>stories</i>.` reads as the emphasis of a word rather than
            # of the sentence it ends.
            whole = start == 0 and last == len(words) - 1
            tail = None if whole else TAIL_PUNCTUATION.search(tagged[last])
            cut = tail.start() if tail and tail.start() else len(tagged[last])
            # Close before opening: where the run is one word long these are
            # the same element, and prepending first would shift `cut`.
            tagged[last] = tagged[last][:cut] + "</i>" + tagged[last][cut:]
            tagged[start] = "<i>" + tagged[start]
            start = None
    return tagged


def count_italics(text):
    """How many `<i>` spans a cue came out with, for `--dry-run` to report."""
    return text.count("<i>")


def strip_italics(text):
    """The same text without its tags, for the steps that read words."""
    return text.replace("<i>", "").replace("</i>", "")


def film_line_height(paths, mapper=map, progress=None):
    """How tall one line of type is across a whole film.

    The median over every band of every frame. Bands are what a film renders
    consistently and a frame does not: a line's band is only as tall as the
    letters that happen to be on it, so a line of `acontece` measures 0.68 of
    the film's median and one carrying brackets and quotes 1.63. That spread is
    why this is a second opinion for `ocr` rather than a replacement for its
    own measurement — every constant here is calibrated against the per-frame
    band, and swapping this in would rescale every cue.

    Also returns the film's italic angle: the median slant of the bands wide
    enough and leaning far enough to be italic on their own account, or None
    where the film showed none. A word is too small a sample for the absolute
    test, so that angle is what `band_italics` matches single words against —
    and a film without one has its word rule switched off rather than pointed
    at a guess.

    Deliberately skips `despeckle` and `deblob`, which cost around five times
    what the rest of this does and move the median not at all: 389/389, 213/212
    and 194/194 on taxonomia, o_que and test_extracted. The same argument
    carries the slant: a median over a whole film does not move for the few
    frames that carry dirt, and the measurement that decides a cue is the
    despeckled one in `ocr`.

    `progress(done, total)` is called once a frame, for a front end that has a
    bar to move. The command line passes nothing and prints nothing, so its
    stdout is unchanged; the page passes one because this pass runs before the
    first cue and would otherwise look like a hang.
    """
    heights, leans = [], []
    for done, (bands, slants) in enumerate(mapper(frame_bands, paths), 1):
        heights += [b - a for a, b in bands]
        leans += [s for s, w in slants if is_italic_band(s, w)]
        if progress is not None:
            progress(done, len(paths))
    return (
        float(np.median(heights)) if heights else 0.0,
        float(np.median(leans)) if leans else None,
    )


def frame_bands(path):
    """One frame's bands and their slants, so the pool hands back neither masks
    nor a second read of the same image.

    Mapping `load_ink` itself would queue every task at once and keep an ink
    mask alive for each; these frames are 8-11k px wide and that is gigabytes.
    """
    ink = load_ink(path)
    bands = row_bands(ink)
    if not bands:
        return [], []
    line_h = float(np.median([b - a for a, b in bands]))
    return bands, band_slants(ink, bands, line_h)


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


def deblob(ink):
    """Drop whole marks that are too fat to be type.

    The mirror of `despeckle`, which throws out dirt that is small and sits
    away from the words: this throws out dirt that is large and sits on them.
    A mark goes whole rather than trimmed, because the thin end of a scratch is
    indistinguishable from a stroke.

    The limit of the idea, worth knowing before tuning it: a blot that merely
    rests inside the bowl of an `o` is its own component and lifts out cleanly,
    while one that touches the stroke is the same component as the letter and
    takes the letter with it. Fatness cannot tell those apart, because a
    filled-in `o` and a blot of ink are the same object.
    """
    if not ink.any():
        return ink, 0
    # Work on the ink's bounding box. These frames are mostly empty margin and
    # the distance transform is the expensive step, so this is ~5x quicker for
    # the same answer: a mark's nearest background is a stroke-width away, far
    # inside the crop.
    rows = np.nonzero(ink.any(axis=1))[0]
    cols = np.nonzero(ink.any(axis=0))[0]
    box = (slice(max(0, rows[0] - 2), rows[-1] + 3),
           slice(max(0, cols[0] - 2), cols[-1] + 3))
    patch = ink[box]
    dist = ndimage.distance_transform_edt(patch)
    stroke = float(np.median(dist[patch]))
    if stroke <= 0:
        return ink, 0
    # Only whether a mark exceeds the limit matters, never by how much, so
    # threshold first and ask which marks the survivors belong to. That is far
    # cheaper than a maximum per component, and on a clean frame — which most
    # frames are — nothing survives and there is nothing left to do.
    core = dist > BLOB_STROKES * stroke
    if not core.any():
        return ink, 0
    labels, count = ndimage.label(patch)
    if not count:
        return ink, 0
    blob = np.zeros(count + 1, bool)
    blob[np.unique(labels[core])] = True
    blob[0] = False
    out = ink.copy()
    out[box] = patch & ~blob[labels]
    return out, int(blob.sum())


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


def ocr(path, lang="eng", film_line_h=0.0, film_slant=None, italics=True):
    """Transcribe one frame, reading it both plain and de-gouged.

    Neither render wins everywhere — closing rescues the drop-shadow face and
    corrupts ordinary digits — so both are read and tesseract's confidence
    picks. Ties go to the plain render, which is right far more often.

    `film_line_h` is how tall a line of type is in this film, from
    `film_line_height`, and is only consulted to catch a band that holds two
    lines joined by a mark. Zero, the default, leaves each frame to speak for
    itself. `film_slant` comes from the same pass and is what a single italic
    word is matched against. Everything else here stays a function of this
    frame alone.

    The italics are measured on the despeckled, deblobbed mask and applied to
    whichever render won, because a mark being sealed or not does not change
    which way the type leans.
    """
    ink = load_ink(path)
    bands = row_bands(ink)
    if not bands:
        return "", 0, 0, 0, 0
    ink, dropped = despeckle(ink, bands)
    ink, blobs = deblob(ink)
    bands = row_bands(ink) or bands
    line_h = float(np.median([b - a for a, b in bands]))
    if film_line_h and line_h > LINE_MERGE_RATIO * film_line_h:
        line_h = film_line_h

    with tempfile.TemporaryDirectory() as workdir:
        plain, plain_conf = read(render(ink, line_h), lang, workdir)
        sealed, sealed_conf = read(
            render(close_gouges(ink, line_h), line_h), lang, workdir
        )
    text = sealed if sealed_conf > plain_conf else plain
    if italics:
        text = mark_italics(text, band_italics(ink, bands, line_h, film_slant))
    return text, len(bands), dropped, blobs, count_italics(text)


def strip_speck_accent(match):
    """A lower-case token whose de-accented form is English lost an accent to dirt.

    A speck sitting above a letter is read as an acute: `doés`, `thé`, `wé`.
    The word list is the gate and it has to be: dropping it de-accents
    `fragmentary`'s `perpétua` too, which is a real Portuguese accent.
    """
    token = match.group(0)
    if not token.islower() or token.isascii():
        return token
    plain = "".join(
        c for c in unicodedata.normalize("NFD", token)
        if unicodedata.category(c) != "Mn"
    )
    return plain if plain != token and plain in english_words() else token


def clean(raw, lang="eng"):
    """Normalize whitespace and fix the few glyph confusions worth fixing."""
    lines = []
    for line in raw.translate(QUOTE_MAP).splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        # A full stop cannot follow a space: punctuation attaches to the word
        # in front of it. One that does is a speck sitting in the gap between
        # two words, which is the same dirt the `_` rule below catches, read as
        # a different mark. Every language: this is typography, not English.
        #
        # A dot at the *start* of a line is left alone, because there it is a
        # degraded ellipsis rather than dirt — `on_the_sea` opens three cues on
        # a real `..`, and the one lone leading dot in the corpus has a sidecar
        # reading `...if I agree with you`.
        line = " ".join(re.sub(r"(?<=\s)\.(?!\.)", "", line).split())
        # A bare `|` or `l` is the pronoun "I", as is either one carrying a
        # contraction (`l'm`, `l've`) or opening `If`. A leading `-` marks the
        # second speaker in a two-line cue, so it doesn't break the word.
        # English only: in other languages these shapes are real words.
        if lang == "eng":
            line = re.sub(r"(?<![^\s-])[|l](?![^\s'])", "I", line)
            line = re.sub(r"(?<![^\s-])[|l](?=')", "I", line)
            line = re.sub(r"(?<![^\s-])[|l]f\b", "If", line)
            # The same confusion running the other way: a lowercase `i` that
            # has lost its dot comes back as `t` or `l`, and neither `ts` nor
            # `ls` is a word standing on its own.
            line = re.sub(r"(?<![^\s-])[tl]s\b(?!')", "is", line)
            # A mark landing in the gap between two words is read as whatever
            # it resembles. An underscore is never part of a word, and a
            # capital inside a lower-case one is not a letter either — except
            # in words that start with a capital (`McDonald`, `YouTube`), so
            # only lower-case-initial words are touched.
            line = re.sub(r"(?<=[A-Za-z])_(?=[A-Za-z])", " ", line)
            # Matched whole so the exception list can be consulted, but only
            # acted on for a capital with lower-case on both sides, which is
            # the shape a mark makes; `tO` at the end of a word is something
            # else and is left alone.
            line = re.sub(
                r"\b[a-z][A-Za-z]*\b",
                lambda m: m.group(0).lower()
                if re.search(r"[a-z][A-Z][a-z]", m.group(0))
                and m.group(0).lower() not in CAMEL_WORDS
                else m.group(0),
                line,
            )
            # Same again for an apostrophe, which is harder because most of
            # them are real. One between two letters is kept only when what
            # follows it is a contraction or a possessive; `about'tWenty` and
            # `Scope'screen` are marks, `don't` and `cinema's` are not. A
            # capital after the apostrophe means a name (`O'Brien`), and one
            # at the end of a word is a plural possessive (`guys'`); both are
            # left alone by requiring a lower-case letter after it. The
            # A single letter fenced by apostrophes is an idiom — `rock'n'roll`,
            # `guns'n'roses`, `Toys'R'Us` — so neither of its apostrophes is
            # touched, whichever letter it is.
            line = re.sub(
                r"(?<=[A-Za-z])(?<!'[A-Za-z])'(?=[a-z])(?![A-Za-z]')"
                r"(?!" + APOSTROPHE_TAILS + r"(?![A-Za-z]))",
                " ", line,
            )
            # A mark fused to a lower-case letter can make a capital of it, the
            # same way one inside a word does. Only in the middle of a
            # sentence, and only for the words on the list: the word before
            # must end in a letter or a comma, which leaves alone anything
            # opening a sentence, a `[speaker]` label, a quotation or the `-`
            # that marks the second speaker.
            line = re.sub(
                r"(?:(?<=[A-Za-z]\s)|(?<=[A-Za-z],\s))([A-Z][a-z]+)\b",
                lambda m: m.group(1).lower()
                if m.group(1).lower() in MID_SENTENCE_WORDS
                else m.group(1),
                line,
            )
            # A speck sitting above a letter is read as an accent, so a
            # lower-case token whose plain-ASCII form is an English word is
            # that speck rather than a foreign word. A token of one letter is
            # excluded, because a lone accented letter is a word in Portuguese
            # (`é`, `à`) and never one in English, and that exclusion is what
            # keeps `fragmentary`'s bilingual text out of this. Fires on four
            # lines in 1765: `doés`, `thé` and `wé` come out right, and one
            # already-mangled `fragmentary` line is no worse.
            line = re.sub(r"[^\W\d_]{2,}", strip_speck_accent, line)
        lines.append(line)
    return "\n".join(lines)


def english_words():
    """The word list, read once. An empty set if it isn't there."""
    if english_words.cache is None:
        try:
            text = LOCAL_WORDS.read_text(encoding="utf-8")
        except OSError:
            english_words.cache = frozenset()
        else:
            english_words.cache = frozenset(
                w for w in text.split() if not w.startswith("#")
            )
    return english_words.cache


english_words.cache = None


def one_edit(word, words):
    """Every word in `words` reachable from `word` by one letter."""
    found = set()
    for i in range(len(word) + 1):
        head, tail = word[:i], word[i:]
        if tail:
            shorter = head + tail[1:]
            if shorter in words:
                found.add(shorter)
        for letter in "abcdefghijklmnopqrstuvwxyz":
            if tail:
                swapped = head + letter + tail[1:]
                if swapped != word and swapped in words:
                    found.add(swapped)
            longer = head + letter + tail
            if longer in words:
                found.add(longer)
    return found


def suspects(text, lang="eng"):
    """Words that are not English but sit one letter from a word that is.

    A report, never a correction. Rewriting these automatically was measured
    and is a bad trade — see "Why the sidecars cannot be automated" in
    CLAUDE.md — but as a list of places to look it costs nothing and finds
    things a read-through misses.

    Only lower-case words are considered, which keeps every proper noun out of
    it, and only when exactly one word is a single letter away, which keeps out
    the ones that are anybody's guess.
    """
    if lang != "eng":
        return []
    words = english_words()
    if not words:
        return []
    found = []
    for token in re.findall(r"[A-Za-z']+", text):
        if not re.fullmatch(r"[a-z]+", token):
            continue
        if len(token) < SUSPECT_LENGTH or token in words:
            continue
        near = one_edit(token, words)
        if len(near) == 1:
            found.append((token, near.pop()))
    return found


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


def stamp_ms(half):
    """Milliseconds from one half of a timestamp line, `00:04:29,960`."""
    hours, minutes, rest = half.split(":")
    seconds, thousandths = rest.split(",")
    return (
        ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000
        + int(thousandths)
    )


def stamp_gap(first, second):
    """Milliseconds of blank between one cue and the next, negative if they overlap."""
    return stamp_ms(second[:12]) - stamp_ms(first[17:])


def stamp_span(first, last):
    """One timestamp line covering both: the start of `first`, the end of `last`."""
    return first[:12] + " --> " + last[17:]


def edit_distance(before, after):
    """Levenshtein, asked only whether two words are the same word misread."""
    if before == after:
        return 0
    previous = list(range(len(after) + 1))
    for i, left in enumerate(before, 1):
        row = [i]
        for j, right in enumerate(after, 1):
            row.append(min(
                previous[j] + 1, row[j - 1] + 1, previous[j - 1] + (left != right)
            ))
        previous = row
    return previous[-1]


def in_word_list(token, words):
    """Is this token an English word, whatever punctuation it arrived wearing?"""
    core = re.sub(r"^[^A-Za-z']+|[^A-Za-z']+$", "", token)
    return bool(core) and (
        core in words or core.lower() in words or core.capitalize() in words
    )


def reconcile(before, after, lang="eng"):
    """The same caption read twice, resolved word by word, or None.

    Two renders of one subtitle come back a word or two apart, and the word list
    can often say which reading is the real one: of `camp` and `eamp` only one is
    English. So where exactly one side of a differing pair is in the list, that
    side wins; where both are (`tear` and `teat`) or neither is (`Tupamaro` and
    `Tiipamaro`), the earlier render is kept, and if *no* differing word at all is
    in the list there is no evidence here and this returns None for the caller to
    report instead.

    The gates in front of that are what keep it off two cues that merely
    resemble each other. The texts must be `MERGE_NEAR_RATIO` alike; they must
    have the same shape, line for line and word for word, which is what excludes
    the four pairs in these films where a second speaker's line appears between
    one cue and the next; half the words must already agree; and each differing
    pair must be within `MERGE_NEAR_EDITS` of its twin, which is a misreading
    rather than a different word.

    English only: this is the word list talking, and there is no Portuguese one.
    Measured over the eleven films it merges three pairs and all three are right
    — see "Step 6" in CLAUDE.md. Its known cost is the tie: where both words are
    real the earlier cue simply wins, on no evidence, so the merge report prints
    the word-level diff of everything this changed.
    """
    if lang != "eng":
        return None
    words = english_words()
    if not words:
        return None
    # Every test below asks the word list a question, so it asks it about the
    # words rather than about the markup: `<i>tear` is in no dictionary. The
    # tags stay on whichever word wins.
    bare = strip_italics
    if (
        difflib.SequenceMatcher(None, bare(before), bare(after)).ratio()
        < MERGE_NEAR_RATIO
    ):
        return None
    left, right = before.split("\n"), after.split("\n")
    if len(left) != len(right):
        return None
    lines = []
    for one, other in zip(left, right):
        words_before, words_after = one.split(), other.split()
        if len(words_before) != len(words_after):
            return None
        lines.append(list(zip(words_before, words_after)))
    differing = [
        pair for line in lines for pair in line if bare(pair[0]) != bare(pair[1])
    ]
    if not differing or len(differing) * 2 > sum(len(line) for line in lines):
        return None
    if any(edit_distance(bare(x), bare(y)) > MERGE_NEAR_EDITS for x, y in differing):
        return None
    if not any(
        in_word_list(bare(x), words) or in_word_list(bare(y), words)
        for x, y in differing
    ):
        return None
    return "\n".join(
        " ".join(
            y
            if bare(x) != bare(y)
            and in_word_list(bare(y), words)
            and not in_word_list(bare(x), words)
            else x
            for x, y in line
        )
        for line in lines
    )


def repeat_notes(stamps, texts, folded, lang):
    """Neighbours that look like one cue but cannot be merged on the evidence.

    A report and nothing else, in the manner of the suspect-word report, and
    keyed to the cue numbers `transcribe` and `--dry-run` already use, which are
    the numbers before anything folds. Two cases: identical text just past the
    merge window, and a near-identical neighbour `reconcile` would not resolve.
    """
    notes = []
    for number, (first, second) in enumerate(zip(stamps, stamps[1:]), 1):
        if second in folded:
            continue
        before, after = texts[first], texts[second]
        gap = stamp_gap(first, second)
        if not before or not after or gap < 0:
            continue
        if before == after:
            if MERGE_GAP_MS < gap <= MERGE_REPORT_MS:
                notes.append(
                    "warning: cue {} and {} carry the same text {} ms apart".format(
                        number, number + 1, gap
                    )
                )
        elif gap <= MERGE_GAP_MS:
            alike = difflib.SequenceMatcher(
                None, strip_italics(before), strip_italics(after)
            ).ratio()
            if alike >= MERGE_NEAR_RATIO and reconcile(before, after, lang) is None:
                notes.append(
                    "warning: cue {} and {} are {:.0f}% alike {} ms apart, "
                    "perhaps one cue the rip split".format(
                        number, number + 1, alike * 100, gap
                    )
                )
    return notes


def merge_repeats(stamps, texts, lang="eng"):
    """Step 6: fold the cues the rip split back into one.

    A subtitle redrawn mid-display becomes several images, several filenames and
    so several cues carrying the same line back to back. A run of neighbours is
    folded while the next one reads the same (or `reconcile`s against what the
    run has read so far) and sits within `MERGE_GAP_MS` of it; the run keeps the
    first cue's start and the last one's end. Overlapping cues and empty text
    never fold, which is also what keeps a template that is not in chronological
    order safe.

    Returns the cue list and texts to render, the `Merge` records to report, and
    the notes for the neighbours that were left alone.
    """
    kept, merged, merges, folded = [], {}, [], set()
    index = 0
    while index < len(stamps):
        run, text, kind = [stamps[index]], texts[stamps[index]], "identical"
        while index + len(run) < len(stamps):
            following = stamps[index + len(run)]
            gap = stamp_gap(run[-1], following)
            joined = None
            if text and texts[following] and 0 <= gap <= MERGE_GAP_MS:
                if texts[following] == text:
                    joined = text
                else:
                    joined = reconcile(text, texts[following], lang)
                    if joined is not None:
                        kind = "reconciled"
            if joined is None:
                break
            text = joined
            run.append(following)
        stamp = run[0] if len(run) == 1 else stamp_span(run[0], run[-1])
        kept.append(stamp)
        merged[stamp] = text
        if len(run) > 1:
            merges.append(Merge(
                stamp, tuple(run), index + 1, kind,
                tuple(word_diff(texts[run[0]], text)),
            ))
            folded.update(run[1:])
        index += len(run)
    return kept, merged, merges, repeat_notes(stamps, texts, folded, lang)


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


def fill(text, texts, merges=()):
    """Replace each block's placeholder with the OCR'd text for its timestamp.

    A merge leaves the template holding more blocks than there are cues: the
    first block of a run takes the merged range and the merged text, the rest
    are dropped, and the numbering the template otherwise supplies is rewritten
    1..N so the file comes out without holes in it. With nothing merged the
    template's own numbers are kept, untouched, which is what keeps the output
    of a film that merges nothing exactly as it was.
    """
    opening = {m.absorbed[0]: m.stamp for m in merges}
    absorbed = {s for m in merges for s in m.absorbed[1:]}
    out_blocks = []
    number = 0
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        stamp = next(
            (l.strip() for l in lines if TIMESTAMP_RE.match(l.strip())), None
        )
        if stamp is None:
            out_blocks.append(block)
            continue
        if stamp in absorbed:
            continue
        number += 1
        merged = opening.get(stamp, stamp)
        body = texts[merged]
        head = lines[: lines.index(next(l for l in lines if l.strip() == stamp)) + 1]
        rest = [l for l in lines[len(head):] if l.strip() != PLACEHOLDER]
        if merges:
            head = [
                str(number) if l.strip().isdigit() else l for l in head[:-1]
            ] + [merged]
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


def transcribe(stamps, by_timestamp, corrections, lang="eng", workers=1,
               progress=None, italics=True):
    """Steps 3 to 5 for every cue, yielding one Cue each, in cue order.

    `workers` only changes how many images are in flight: `ocr` is a pure
    function of its arguments and builds its own temporary directory, and
    Executor.map yields in *input* order, so the corrections and the warnings
    still come out in strict cue order and the output is unchanged.

    The images are read twice: once cheaply, for the film's line height, and
    then for real. That first pass is what makes a cue's text depend on the
    other images in the folder rather than on itself alone — run this over half
    a film and a cue on the edge of `LINE_MERGE_RATIO` can come out
    differently. It costs a few per cent of the run: 0.011s a cue on
    test_extracted, 0.115s on taxonomia, against 0.4-0.8s for the OCR itself.
    `progress` is handed to it so a front end can show that pass moving.
    """
    paths = [by_timestamp[s] for s in stamps]
    pool = None
    try:
        if workers > 1:
            # Tesseract uses OpenMP internally; several multithreaded copies of
            # it would fight over the same cores for a net loss.
            os.environ.setdefault("OMP_THREAD_LIMIT", "1")
            pool = concurrent.futures.ThreadPoolExecutor(workers)
            film_line_h, film_slant = film_line_height(paths, pool.map, progress)
            results = pool.map(
                ocr, paths, itertools.repeat(lang), itertools.repeat(film_line_h),
                itertools.repeat(film_slant), itertools.repeat(italics),
            )
        else:
            film_line_h, film_slant = film_line_height(paths, progress=progress)
            results = (
                ocr(path, lang, film_line_h, film_slant, italics)
                for path in paths
            )

        for number, (stamp, path, (text, want, dropped, blobs, marked)) in enumerate(
            zip(stamps, paths, results), 1
        ):
            changes = None
            # A sidecar entry is written as plain text, so it is compared
            # against the words rather than against the markup. An entry that
            # agrees with the OCR stays the silent no-op it always was and
            # keeps the italics; one that changes a word replaces the text
            # whole, italics included, and a person wanting them back writes
            # the tags into the sidecar themselves.
            if stamp in corrections and corrections[stamp] != strip_italics(text):
                changes = word_diff(strip_italics(text), corrections[stamp])
                text = corrections[stamp]
            got = len(text.splitlines())
            # The line-count check runs one way only. A band is a run of inked
            # rows and no blank row can fall inside a line of text, so the band
            # count can never overcount lines — `got > want` means two lines
            # shared a band, which is a fact about the geometry rather than
            # about the transcription, and warning on it cried wolf 44 times in
            # eleven films. `got < want` is the sound direction: a band of ink
            # came back with no line of text in it. It fires on 2 cues of 1336
            # and both are real — a line lost to dirt, and a line set in outline
            # type that OCR'd to nothing.
            # Point at the cue's number rather than its filename: that is what
            # a person checking the result has in front of them.
            if not text:
                warning = "warning: cue {} OCR'd to nothing".format(number)
            elif got < want:
                warning = "warning: cue {} read {} line(s) from {} bands of ink".format(
                    number, got, want
                )
            else:
                warning = None
            yield Cue(number, stamp, path, text, want, got, dropped, blobs, marked,
                      changes, warning, suspects(strip_italics(text), lang))
    finally:
        if pool is not None:
            pool.shutdown(wait=False)


def render_srt(template, stamps, texts, newline, merges=()):
    """The finished SRT as bytes, with the line endings the caller asked for."""
    body = fill(template, texts, merges) if template else build(stamps, texts)
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
        "--no-merge",
        action="store_true",
        help="keep every image as its own cue, even when the rip split one subtitle",
    )
    ap.add_argument(
        "--no-italics",
        action="store_true",
        help="do not mark italic type with <i>",
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
        p.stamps, p.by_timestamp, p.corrections, args.lang, args.jobs,
        italics=not args.no_italics,
    ):
        texts[cue.stamp] = cue.text
        if cue.changes is not None:
            applied.append((cue.stamp, cue.changes))
        if cue.warning:
            print(cue.warning, file=sys.stderr)
            warnings += 1
        for word, near in cue.suspects:
            print('warning: cue {} reads "{}", perhaps "{}"'.format(
                cue.number, word, near), file=sys.stderr)
            warnings += 1
        if args.dry_run:
            notes = []
            if cue.dropped:
                notes.append("{} speck removed".format(cue.dropped))
            if cue.blobs:
                notes.append("{} blob removed".format(cue.blobs))
            if cue.marked:
                notes.append("{} italic".format(cue.marked))
            print("{}{}\n{}\n".format(
                cue.stamp,
                "  [{}]".format(", ".join(notes)) if notes else "",
                cue.text,
            ))

    images = len(texts)
    stamps, merges = p.stamps, ()
    if not args.no_merge:
        stamps, texts, merges, notes = merge_repeats(p.stamps, texts, args.lang)
        for note in notes:
            print(note, file=sys.stderr)
            warnings += 1

    if applied:
        print("\ncorrections applied ({} cue(s)):".format(len(applied)), file=sys.stderr)
        for stamp, changes in applied:
            for was, now in changes:
                print("  {}  {!r} -> {!r}".format(stamp[:12], was, now), file=sys.stderr)

    if merges:
        print("\nmerged {} cue(s) into {}:".format(
            sum(len(m.absorbed) for m in merges), len(merges)), file=sys.stderr)
        for m in merges:
            # Say when the word list was what settled it: a reconciled run has
            # thrown a reading away, and where the two readings tied it did so
            # without a diff to show for it.
            print("  cues {}-{} -> {}{}".format(
                m.number, m.number + len(m.absorbed) - 1, m.stamp,
                "  (reconciled)" if m.kind == "reconciled" else ""),
                file=sys.stderr)
            for was, now in m.changes:
                print("      {!r} -> {!r}".format(was, now), file=sys.stderr)

    if args.dry_run:
        print(
            "\n{} image(s), {} warning(s); nothing written".format(images, warnings),
            file=sys.stderr,
        )
        return

    out_path.write_bytes(render_srt(p.template, stamps, texts, p.newline, merges))
    print(
        "wrote {} ({} cues, {} warning(s))".format(out_path, len(texts), warnings)
    )


if __name__ == "__main__":
    main()

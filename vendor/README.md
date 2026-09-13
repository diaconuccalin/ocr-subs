# Vendored runtimes

Committed rather than fetched from a CDN, for three reasons: the page is then
self-contained and reproducible, a cross-origin-isolated page would need CORP
headers that a CDN will not always send, and nothing the page does involves a
request to a third party.

Nothing here is this project's work. Each component keeps its own licence.

| directory | component | version | licence |
|---|---|---|---|
| `pyodide/` | [Pyodide](https://pyodide.org/) — CPython, numpy, scipy, Pillow compiled to WebAssembly | 314.0.6 | MPL-2.0 |
| `tesseract/` | [tesseract.js](https://github.com/naptha/tesseract.js) and its `tesseract.js-core` WebAssembly build | 7.0.0 | Apache-2.0 |
| `tessdata/` | [tesseract language data](https://github.com/tesseract-ocr/tessdata), gzipped: `eng` as shipped by Debian's `tesseract-ocr-eng`, `por` from `tessdata_best` | — | Apache-2.0 |
| `fonts/` | the three faces the page's design uses — [Newsreader](https://fonts.google.com/specimen/Newsreader), [Instrument Sans](https://fonts.google.com/specimen/Instrument+Sans), [JetBrains Mono](https://fonts.google.com/specimen/JetBrains+Mono) | — | OFL-1.1 |

The fonts are the same variable WOFF2 files Google Fonts serves, `latin` and
`latin-ext` only, with `fonts.css` rewritten to point at them here. The reason
is the one at the top of this file: `fonts.gstatic.com` is a third party, and a
stylesheet fetched `no-cors` from it is blocked outright once the page is
cross-origin isolated. The five icons the page draws are Material Symbols
Outlined (Apache-2.0) and are inlined as SVG paths in `index.html` and `app.js`
rather than vendored, since a webfont for five glyphs costs more than the
glyphs.

`../coi-serviceworker.js` is [coi-serviceworker](https://github.com/gzuidhof/coi-serviceworker)
by Guido Zuidhof and contributors, MIT.

The Python packages inside the Pyodide wheels carry their own licences: NumPy and
SciPy BSD-3-Clause, Pillow MIT-CMU, CPython PSF-2.0.

## Which tesseract cores are kept, and why all three

tesseract.js picks its core along two axes. The **engine** axis follows the OEM
we ask for: the pipeline always passes OEM 1, so only the `-lstm` builds are
ever requested and the legacy-engine ones would be dead weight. The **SIMD**
axis follows what the visitor's browser can do, and there are three tiers:

| file | chosen when |
|---|---|
| `tesseract-core-lstm.*` | no WebAssembly SIMD |
| `tesseract-core-simd-lstm.*` | fixed-width SIMD |
| `tesseract-core-relaxedsimd-lstm.*` | relaxed SIMD (recent Chrome, Edge) |

**All three must be present.** A browser that asks for a tier we did not ship
fails at the first cue with `importScripts ... failed to load`, and nothing
earlier in the run gives any warning. This happened: the page was first tested
on a Chromium old enough to want the fixed-width build, and shipping only that
one broke every newer Chrome. If tesseract.js is ever upgraded, re-copy all
three tiers together.

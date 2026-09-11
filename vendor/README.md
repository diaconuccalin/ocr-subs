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

`../coi-serviceworker.js` is [coi-serviceworker](https://github.com/gzuidhof/coi-serviceworker)
by Guido Zuidhof and contributors, MIT.

The Python packages inside the Pyodide wheels carry their own licences: NumPy and
SciPy BSD-3-Clause, Pillow MIT-CMU, CPython PSF-2.0.

Only the LSTM tesseract cores are kept (`-lstm` and `-simd-lstm`), since the
pipeline asks for OEM 1; the legacy-engine builds would be dead weight.

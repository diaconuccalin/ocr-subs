// Runs the project's own ocr_subs.py, unmodified, under Pyodide.
//
// The one thing WebAssembly cannot do is start the `tesseract` process that
// `read` shells out to, so `ocr_subs.TESSERACT` is rebound to a function that
// hands the render to the page and waits for the reply. Waiting is the whole
// difficulty: Python is synchronous and tesseract.js is promise-based, so the
// wait is a real one — Atomics.wait on a SharedArrayBuffer, which is why the
// page needs to be cross-origin isolated and why this runs in a worker, where
// blocking is allowed and harms nobody.

import { loadPyodide } from "./vendor/pyodide/pyodide.mjs";

const CTL = 4;
const decoder = new TextDecoder();
let ctl, data, py;

function tesseract(png, lang) {
  const bytes = png.toJs ? png.toJs() : png;
  if (bytes.length > data.length) {
    throw new Error("render of " + bytes.length + " bytes is too large to pass");
  }
  data.set(bytes, 0);
  Atomics.store(ctl, 0, 1);
  self.postMessage({ type: "ocr", lang, length: bytes.length });
  Atomics.wait(ctl, 0, 1);
  const n = Atomics.load(ctl, 1);
  const failed = Atomics.load(ctl, 2);
  Atomics.store(ctl, 0, 0);
  // .slice, not .subarray: a view onto shared memory is not something
  // TextDecoder will accept, so the bytes have to be copied out first.
  const text = decoder.decode(data.slice(0, n));
  if (failed) throw new Error(text);
  return text;
}

// Python source, carried in a template literal: nothing in here may contain a
// backtick, which closes the string and leaves the whole worker unparseable —
// so quote names in plain prose rather than in the Markdown the rest of the
// project writes.
const DRIVER = `
import json, sys
sys.path.insert(0, "/code")
import ocr_subs

def _tesseract(png, lang, workdir):
    return js_tesseract(png, lang)

ocr_subs.TESSERACT = _tesseract
_state = {}

def do_plan(image_dir, srt_name):
    """Steps 1 and 2. Fatal problems come back as data, not as a traceback."""
    notes = []
    try:
        p = ocr_subs.plan(
            ocr_subs.Path(image_dir),
            srt_name or None,
            no_corrections=True,
            report=notes.append,
        )
    except SystemExit as exc:
        return json.dumps({"ok": False, "error": str(exc), "notes": notes})
    _state["plan"] = p
    return json.dumps({
        "ok": True,
        "cues": len(p.stamps),
        "suggested": p.default_out.name,
        "notes": notes,
    })

def do_run(lang):
    """Steps 3 to 7, reporting each cue as it lands.

    transcribe reads every image once for the film's line height before the
    first cue lands, so that pass reports too: on a long film it is a minute of
    work with nothing to show for it, and a still bar reads as a hang.

    The merge at the end folds the cues the rip split, so the file holds fewer
    cues than the folder holds images and the page is told both numbers. It runs
    on raw OCR here, with no sidecar behind it, so a film merges fewer cues in
    the browser than on the command line.
    """
    p = _state["plan"]
    texts, warnings, done = {}, 0, 0
    for cue in ocr_subs.transcribe(p.stamps, p.by_timestamp, p.corrections, lang,
                                   progress=js_measure):
        texts[cue.stamp] = cue.text
        if cue.warning:
            js_warn(cue.warning)
            warnings += 1
        for word, near in cue.suspects:
            js_warn('warning: cue %d reads "%s", perhaps "%s"'
                    % (cue.number, word, near))
            warnings += 1
        done += 1
        js_progress(done)
    stamps, texts, merges, notes = ocr_subs.merge_repeats(p.stamps, texts, lang)
    for note in notes:
        js_warn(note)
        warnings += 1
    for merge in merges:
        js_warn("merged cue %d to %d, which the rip split"
                % (merge.number, merge.number + len(merge.absorbed) - 1))
    with open("/out.srt", "wb") as f:
        f.write(ocr_subs.render_srt(p.template, stamps, texts, p.newline, merges))
    return json.dumps({"warnings": warnings, "cues": len(texts)})
`;

// A directory name from the browser is never allowed to steer where a file lands.
function safeName(name) {
  const base = String(name).replace(/\\/g, "/").split("/").pop();
  if (!base || base === "." || base === ".." || base.includes("\0")) return null;
  return base;
}

async function init(sab) {
  ctl = new Int32Array(sab, 0, CTL);
  data = new Uint8Array(sab, CTL * 4);
  // The 35 MB arrives in three distinguishable pieces, so say which one is in
  // flight. One message that never changes cannot be told apart from a hang,
  // and that is the only thing the page had to say for a minute-long download.
  const stage = (text) => self.postMessage({ type: "stage", text });
  stage("Starting Python… (13 MB)");
  py = await loadPyodide({
    indexURL: new URL("./vendor/pyodide/", self.location).href,
    stdout: (line) => self.postMessage({ type: "debug", line }),
    stderr: (line) => self.postMessage({ type: "debug", line }),
  });
  stage("Loading numpy, scipy and Pillow… (18 MB)");
  await py.loadPackage(["numpy", "scipy", "Pillow"], {
    messageCallback: (line) => self.postMessage({ type: "debug", line }),
    errorCallback: (line) => self.postMessage({ type: "debug", line }),
  });
  stage("Fetching the pipeline…");

  // The page serves the very same file the command line runs, and the word
  // list it looks for beside itself.
  const [source, words] = await Promise.all([
    fetch("./ocr_subs.py").then((r) => r.text()),
    fetch("./words.txt").then((r) => r.text()),
  ]);
  py.FS.mkdirTree("/code");
  py.FS.writeFile("/code/ocr_subs.py", source);
  py.FS.writeFile("/code/words.txt", words);
  py.globals.set("js_tesseract", tesseract);
  py.globals.set("js_warn", (line) => self.postMessage({ type: "warning", line }));
  py.globals.set("js_progress", (done) => self.postMessage({ type: "cue", done }));
  py.globals.set("js_measure", (done, total) =>
    self.postMessage({ type: "measure", done, total }));
  py.runPython(DRIVER);
  self.postMessage({ type: "ready" });
}

async function load({ files, template, folder }) {
  const dir = "/work/" + (safeName(folder) || "subtitles");
  try { py.FS.unmount("/work"); } catch (e) { /* nothing mounted yet */ }
  for (const p of ["/work", dir]) { try { py.FS.mkdir(p); } catch (e) { /* exists */ } }
  for (const name of py.FS.readdir(dir)) {
    if (name !== "." && name !== "..") py.FS.unlink(dir + "/" + name);
  }

  let written = 0;
  for (const file of files) {
    const name = safeName(file.name);
    if (!name) continue;
    py.FS.writeFile(dir + "/" + name, new Uint8Array(await file.arrayBuffer()));
    written += 1;
    if (written % 25 === 0) self.postMessage({ type: "loading", done: written });
  }
  let templateName = "";
  if (template) {
    templateName = safeName(template.name);
    py.FS.writeFile(dir + "/" + templateName,
      new Uint8Array(await template.arrayBuffer()));
  }
  const summary = py.runPython(
    `do_plan(${JSON.stringify(dir)}, ${JSON.stringify(templateName)})`
  );
  self.postMessage({ type: "planned", summary: JSON.parse(summary) });
}

async function handle(message) {
  if (message.type === "init") {
    await init(message.sab);
  } else if (message.type === "load") {
    await load(message);
  } else if (message.type === "run") {
    const fn = py.globals.get("do_run");
    // A merge can leave fewer cues in the file than there were images, so the
    // run reports both numbers rather than only how many it read.
    const summary = JSON.parse(fn(message.lang));
    fn.destroy();
    self.postMessage({
      type: "done",
      srt: py.FS.readFile("/out.srt"),
      warnings: summary.warnings,
      cues: summary.cues,
    });
  }
}

// A worker's event loop starts the next message without waiting for the last
// handler's promise, so a folder picked while the runtime was still downloading
// used to run against a half-built interpreter — far enough along for the
// filesystem to work, not far enough for the driver to be defined. Every
// message waits its turn instead.
let queue = Promise.resolve();

self.onmessage = (event) => {
  const message = event.data;
  queue = queue.then(() => handle(message)).catch((error) => {
    self.postMessage({ type: "failed", error: String((error && error.message) || error) });
  });
};

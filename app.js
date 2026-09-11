// The page: picks the files, drives the Python worker, reports progress, saves.
// The OCR itself lives here on the main thread rather than in a third worker,
// because tesseract.js starts a worker of its own and nesting those is a recent
// ability in some browsers. Nothing here blocks: the Python worker does the
// waiting, which is exactly what a worker is for.

const CTL = 4;                 // control slots: state, length, error flag, spare
const CAPACITY = 4 << 20;      // the largest render measured is 24 KB
const SEED_SECONDS_PER_CUE = 0.75;   // measured; replaced by the real rate after a few cues

const els = {};
for (const id of ["unsupported", "input-images", "input-srt", "status-images",
                  "status-srt", "drop-images", "drop-srt", "lang", "start",
                  "report", "progress", "bar-fill", "phase", "eta", "log",
                  "log-box", "log-count"]) {
  els[id] = document.getElementById(id);
}

const state = {
  images: [], template: null, templateAuto: false, folder: "",
  cues: 0, suggested: "subtitles.srt", ready: false, running: false,
};

const encoder = new TextEncoder();
let sab, ctl, data, worker, tessWorker, tessLang = null;

// ---------------------------------------------------------------- reporting

function say(kind, title, detail) {
  els.report.hidden = false;
  els.report.className = "banner" + (kind ? " " + kind : "");
  els.report.innerHTML = "";
  const strong = document.createElement("strong");
  strong.textContent = title;
  els.report.append(strong);
  if (detail && detail.length) {
    const pre = document.createElement("pre");
    pre.textContent = Array.isArray(detail) ? detail.join("\n") : detail;
    els.report.append(pre);
  }
}

let logLines = 0;
function log(line) {
  logLines += 1;
  els["log-box"].hidden = false;
  els["log-count"].textContent = logLines;
  els.log.textContent += line + "\n";
  els.log.scrollTop = els.log.scrollHeight;
}

// ------------------------------------------------------------------- timing

function humanTime(seconds) {
  if (seconds < 10) return "a few seconds left";
  if (seconds < 120) return "about " + Math.round(seconds / 10) * 10 + "s left";
  if (seconds < 600) return "about " + Math.round(seconds / 30) * 30 / 60 + " min left";
  return "about " + Math.round(seconds / 60) + " min left";
}

const clock = {
  start(total) {
    this.total = total; this.done = 0; this.seen = 0;
    this.rate = SEED_SECONDS_PER_CUE; this.last = performance.now();
    this.shown = Infinity; this.painted = 0;
  },
  tick(done) {
    const now = performance.now();
    const dt = (now - this.last) / 1000;
    this.last = now;
    this.seen += 1;
    // An exponential average, because per-cue cost varies with image width far
    // too much for a running mean to keep up.
    this.rate = this.seen <= 3 ? dt : this.rate * 0.8 + dt * 0.2;
    this.done = done;
    if (now - this.painted < 1000 && done < this.total) return;
    this.painted = now;
    const eta = (this.total - done) * this.rate;
    // Let the estimate fall freely but only rise when it really has changed,
    // or it jitters by a second every second and looks broken.
    if (eta < this.shown || eta > this.shown * 1.15) this.shown = eta;
    els.eta.textContent = humanTime(this.shown);
  },
};

function phase(text, fraction) {
  els.progress.hidden = false;
  els.phase.textContent = text;
  if (fraction !== undefined) els["bar-fill"].style.width = (fraction * 100) + "%";
}

// -------------------------------------------------------------------- OCR

async function getTesseract(lang) {
  if (tessLang === lang) return tessWorker;
  if (tessWorker) await tessWorker.terminate();
  tessWorker = await Tesseract.createWorker(lang, 1, {
    corePath: "vendor/tesseract/core",
    workerPath: "vendor/tesseract/worker.min.js",
    langPath: "vendor/tessdata",
    logger: () => {},
  });
  // --psm 6, "a uniform block of text": the mode that keeps the line breaks
  // that make a two-line cue come back as two lines.
  await tessWorker.setParameters({ tessedit_pageseg_mode: "6" });
  tessLang = lang;
  return tessWorker;
}

function reply(bytes, failed) {
  const n = Math.min(bytes.length, data.length);
  data.set(bytes.subarray(0, n), 0);
  Atomics.store(ctl, 1, n);
  Atomics.store(ctl, 2, failed);
  Atomics.store(ctl, 0, 2);
  Atomics.notify(ctl, 0);
}

async function serveOcr(message) {
  try {
    const png = new Blob([data.slice(0, message.length)], { type: "image/png" });
    const engine = await getTesseract(message.lang);
    const result = await engine.recognize(png, {}, { tsv: true });
    reply(encoder.encode(result.data.tsv), 0);
  } catch (error) {
    reply(encoder.encode("tesseract.js: " + ((error && error.message) || error)), 1);
  }
}

// ------------------------------------------------------------------ picking

const IMAGE = /\.jpe?g$/i;

async function filesFromDrop(dataTransfer) {
  const entries = [...dataTransfer.items]
    .map((item) => item.webkitGetAsEntry && item.webkitGetAsEntry())
    .filter(Boolean);
  if (!entries.length) return [...dataTransfer.files];
  const out = [];
  const walk = (entry, path) => new Promise((resolve) => {
    if (entry.isFile) {
      entry.file((file) => { file._path = path + file.name; out.push(file); resolve(); },
                 () => resolve());
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const batch = () => reader.readEntries(async (list) => {
        if (!list.length) return resolve();
        await Promise.all(list.map((e) => walk(e, path + entry.name + "/")));
        batch();
      }, () => resolve());
      batch();
    } else resolve();
  });
  await Promise.all(entries.map((e) => walk(e, "")));
  return out;
}

async function acceptImages(files) {
  const images = files.filter((f) => IMAGE.test(f.name));
  if (!images.length) {
    say("error", "No .jpeg images in that selection.");
    return;
  }
  state.images = images;
  const first = images[0];
  const path = first.webkitRelativePath || first._path || "";
  state.folder = path.includes("/") ? path.split("/")[0] : "";

  // Every film folder here holds both the template and a previous filled copy,
  // so picking blind would be wrong half the time: take the one that still has
  // placeholders in it.
  if (!state.template || state.templateAuto) {
    state.template = null; state.templateAuto = false;
    const candidates = files.filter((f) => /\.srt$/i.test(f.name));
    const templates = [];
    for (const file of candidates) {
      if ((await file.text()).includes("[sub_duration]")) templates.push(file);
    }
    if (templates.length === 1) {
      state.template = templates[0];
      state.templateAuto = true;
    }
  }
  els["status-images"].textContent =
    images.length + " image(s)" + (state.folder ? " from " + state.folder : "");
  els["status-images"].className = "frame-status ok";
  revalidate();
}

function acceptTemplate(file) {
  state.template = file;
  state.templateAuto = false;
  revalidate();
}

function showTemplate() {
  if (!state.template) {
    els["status-srt"].textContent = "None — the cues will be numbered from the filenames.";
    els["status-srt"].className = "frame-status";
  } else {
    els["status-srt"].textContent =
      state.template.name + (state.templateAuto ? " (found in the folder)" : "");
    els["status-srt"].className = "frame-status ok";
  }
}

// --------------------------------------------------------------- validating

function revalidate() {
  showTemplate();
  if (!state.images.length) return;
  state.ready = false;
  els.start.disabled = true;
  phase("Checking the cues…");
  els["bar-fill"].style.width = "0%";
  worker.postMessage({
    type: "load",
    files: state.images,
    template: state.template,
    folder: state.folder,
  });
}

function planned(summary) {
  els.progress.hidden = true;
  if (!summary.ok) {
    say("error", summary.error, summary.notes);
    els.start.disabled = true;
  } else {
    state.cues = summary.cues;
    state.suggested = summary.suggested;
    const note = summary.notes.length ? summary.notes : null;
    say(note ? "" : "ok",
        summary.cues + " cue(s) ready" + (note ? ", with warnings:" : "."), note);
    state.ready = true;
    els.start.disabled = false;
  }
}

// ------------------------------------------------------------------ running

async function save(bytes, handle) {
  const blob = new Blob([bytes], { type: "application/x-subrip" });
  if (handle) {
    const stream = await handle.createWritable();
    await stream.write(blob);
    await stream.close();
    return "Saved to " + handle.name + ".";
  }
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = state.suggested;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  return "Downloaded " + state.suggested +
         " — this browser cannot offer a save dialog, so it went to your downloads.";
}

els.start.addEventListener("click", async () => {
  if (!state.ready || state.running) return;
  // Must be the first thing the handler does: a moment later the click no
  // longer counts as the gesture that permits a file dialog.
  let handle = null;
  if (window.showSaveFilePicker) {
    try {
      handle = await window.showSaveFilePicker({
        suggestedName: state.suggested,
        types: [{ description: "SubRip subtitle", accept: { "application/x-subrip": [".srt"] } }],
      });
    } catch (error) {
      return;   // the user changed their mind
    }
  }
  state.running = true;
  els.start.disabled = true;
  els.report.hidden = true;
  clock.start(state.cues);
  phase("Reading 0 of " + state.cues, 0);
  worker.postMessage({ type: "run", lang: els.lang.value });
  worker._handle = handle;
});

async function finished(message) {
  state.running = false;
  els.start.disabled = false;
  phase("Reading " + state.cues + " of " + state.cues, 1);
  els.eta.textContent = "";
  let where;
  try {
    where = await save(message.srt, worker._handle);
  } catch (error) {
    say("error", "Could not save the file", String((error && error.message) || error));
    return;
  }
  log(state.cues + " cue(s), " + message.warnings + " warning(s)");
  say(message.warnings ? "" : "ok",
      state.cues + " cue(s), " + message.warnings + " warning(s). " + where);
}

// -------------------------------------------------------------------- setup

async function main() {
  const config = await (await fetch("langs.json")).json();
  for (const entry of config.languages) {
    const option = document.createElement("option");
    option.value = entry.code;
    option.textContent = entry.name;
    option.selected = entry.code === config.default;
    els.lang.append(option);
  }

  if (!self.crossOriginIsolated || typeof SharedArrayBuffer === "undefined") {
    els.unsupported.hidden = false;
    els.unsupported.textContent =
      "This page needs cross-origin isolation to run Python and tesseract together. " +
      "It normally arranges that itself and reloads once — if you are seeing this, " +
      "the service worker could not register. Reload the page, and check that the " +
      "site is served over https (or from localhost) with service workers allowed.";
    return;
  }

  sab = new SharedArrayBuffer(CTL * 4 + CAPACITY);
  ctl = new Int32Array(sab, 0, CTL);
  data = new Uint8Array(sab, CTL * 4);

  worker = new Worker("worker-py.js", { type: "module" });
  worker.onmessage = async (event) => {
    const message = event.data;
    if (message.type === "ocr") await serveOcr(message);
    else if (message.type === "ready") phase("Ready.", 0), els.progress.hidden = true;
    else if (message.type === "loading") phase("Reading the folder… " + message.done);
    else if (message.type === "planned") planned(message.summary);
    else if (message.type === "cue") {
      clock.tick(message.done);
      phase("Reading " + message.done + " of " + state.cues, message.done / state.cues);
    } else if (message.type === "warning") log(message.line);
    else if (message.type === "debug") console.log("[python]", message.line);
    else if (message.type === "done") await finished(message);
    else if (message.type === "failed") {
      state.running = false;
      els.start.disabled = !state.ready;
      els.progress.hidden = true;
      say("error", "The run stopped", message.error);
    }
  };
  phase("Starting Python…");
  worker.postMessage({ type: "init", sab });

  els["input-images"].addEventListener("change", (e) => acceptImages([...e.target.files]));
  els["input-srt"].addEventListener("change", (e) => {
    if (e.target.files[0]) acceptTemplate(e.target.files[0]);
  });
  for (const [id, handler] of [["drop-images", acceptImages],
                               ["drop-srt", (f) => acceptTemplate(f[0])]]) {
    const zone = els[id];
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("over"));
    zone.addEventListener("drop", async (e) => {
      e.preventDefault();
      zone.classList.remove("over");
      const files = await filesFromDrop(e.dataTransfer);
      if (id === "drop-srt") {
        const srt = files.filter((f) => /\.srt$/i.test(f.name));
        if (srt.length) handler(srt);
      } else handler(files);
    });
  }
}

main();

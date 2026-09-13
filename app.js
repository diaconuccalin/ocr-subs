// The page: picks the files, drives the Python worker, reports progress, saves.
// The OCR itself lives here on the main thread rather than in a third worker,
// because tesseract.js starts a worker of its own and nesting those is a recent
// ability in some browsers. Nothing here blocks: the Python worker does the
// waiting, which is exactly what a worker is for.

const CTL = 4;                 // control slots: state, length, error flag, spare
const CAPACITY = 4 << 20;      // the largest render measured is 24 KB
const SEED_SECONDS_PER_CUE = 0.75;   // measured; replaced by the real rate after a few cues

const els = {};
for (const id of ["unsupported", "input-images", "status-images",
                  "drop-images", "lang", "start",
                  "report", "progress", "bar-fill", "phase", "eta", "log",
                  "log-box", "log-count", "theme", "theme-icon", "theme-label"]) {
  els[id] = document.getElementById(id);
}

const state = {
  images: [], folder: "",
  cues: 0, suggested: "subtitles.srt", ready: false, running: false,
  runtimeReady: false,
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

// -------------------------------------------------------------------- theme

// Material Symbols Outlined, Apache-2.0. Keyed by the theme the button would
// take you to, which is what it names: a moon offers the dark one.
const THEME_ICON = {
  dark: '<svg viewBox="0 -960 960 960"><path d="M480-120q-150 0-255-105T120-480q0-150 105-255t255-105q14 0 27.5 1t26.5 3q-41 29-65.5 75.5T444-660q0 90 63 153t153 63q55 0 101-24.5t75-65.5q2 13 3 26.5t1 27.5q0 150-105 255T480-120Zm0-80q88 0 158-48.5T740-375q-20 5-40 8t-40 3q-123 0-209.5-86.5T364-660q0-20 3-40t8-40q-78 32-126.5 102T200-480q0 116 82 198t198 82Zm-10-270Z"/></svg>',
  light: '<svg viewBox="0 -960 960 960"><path d="M565-395q35-35 35-85t-35-85q-35-35-85-35t-85 35q-35 35-35 85t35 85q35 35 85 35t85-35Zm-226.5 56.5Q280-397 280-480t58.5-141.5Q397-680 480-680t141.5 58.5Q680-563 680-480t-58.5 141.5Q563-280 480-280t-141.5-58.5ZM200-440H40v-80h160v80Zm720 0H760v-80h160v80ZM440-760v-160h80v160h-80Zm0 720v-160h80v160h-80ZM256-650l-101-97 57-59 96 100-52 56Zm492 496-97-101 53-55 101 97-57 59Zm-98-550 97-101 59 57-100 96-56-52ZM154-212l101-97 55 53-97 101-59-57Zm326-268Z"/></svg>',
};

// The attribute is what the stylesheet reads; absent means "follow the system",
// which is the state the page starts in and can never return to once the
// visitor has chosen. The button always names where it would take you.
function currentTheme() {
  const chosen = document.documentElement.dataset.theme;
  if (chosen === "dark" || chosen === "light") return chosen;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark" : "light";
}

function paintThemeButton() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  els["theme-label"].textContent = next === "dark" ? "Dark" : "Light";
  els["theme-icon"].innerHTML = THEME_ICON[next];
  els.theme.title = "Switch to the " + next + " theme";
}

els.theme.addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("ocrsubs-theme", next); } catch (error) {}
  paintThemeButton();
});

// While the visitor has made no choice, the system's preference still rules.
if (window.matchMedia) {
  window.matchMedia("(prefers-color-scheme: dark)")
        .addEventListener("change", paintThemeButton);
}
paintThemeButton();

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

  els["status-images"].textContent =
    images.length + " image(s)" + (state.folder ? " from " + state.folder : "");
  els["status-images"].className = "frame-status ok";
  revalidate();
}

// --------------------------------------------------------------- validating

function revalidate() {
  if (!state.images.length) return;
  state.ready = false;
  els.start.disabled = true;
  phase(state.runtimeReady ? "Checking the cues…"
                           : "Waiting for the runtime, then checking the cues…");
  els["bar-fill"].style.width = "0%";
  worker.postMessage({
    type: "load",
    files: state.images,
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
  els.eta.textContent = "";
  phase("Measuring the type…", 0);
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
  // The file can hold fewer cues than the folder held images, because the run
  // merges back the cues the rip split; say so rather than report the images.
  const written = message.cues || state.cues;
  const folded = state.cues - written;
  const summary = written + " cue(s), " + message.warnings + " warning(s)"
                + (folded ? ", " + folded + " merged" : "");
  log(summary);
  say(message.warnings ? "" : "ok", summary + ". " + where);
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
    else if (message.type === "ready") {
      state.runtimeReady = true;
      if (!state.images.length) { phase("Ready.", 0); els.progress.hidden = true; }
    }
    else if (message.type === "stage") phase(message.text);
    else if (message.type === "loading") phase("Reading the folder… " + message.done);
    else if (message.type === "planned") planned(message.summary);
    else if (message.type === "measure") {
      // The line-height pass, before the first cue: every image is read once
      // with nothing to show for it, so say what it is rather than sit still.
      phase("Measuring the type… " + message.done + " of " + message.total,
            message.done / message.total);
      if (message.done === message.total) clock.start(state.cues);
    }
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
  // A worker that fails to parse or import never sends a message at all, and
  // the page used to sit on the download line for ever with nothing to say.
  worker.onerror = (event) => {
    event.preventDefault();
    say("error", "The Python worker could not start",
        (event.message || "worker error") +
        (event.filename ? "\n" + event.filename + ":" + event.lineno : ""));
    els.progress.hidden = true;
  };
  worker.onmessageerror = () => say("error", "A message from the worker could not be read");

  phase("Downloading Python and the OCR engine… (about 35 MB, once)");
  worker.postMessage({ type: "init", sab });
  // 35 MB is a minute on a slow line and ten seconds on a fast one; past two
  // it is not slow, it is stuck, and the two look identical from here.
  setTimeout(() => {
    if (!state.runtimeReady) {
      say("", "This is taking longer than it should",
          "The runtime is about 35 MB and is normally in the cache after the first visit. " +
          "If it does not arrive: reload with a hard refresh (Ctrl-Shift-R), and if that " +
          "does not do it, unregister the service worker for this page in the browser's " +
          "developer tools and reload. The console shows which file is outstanding.");
    }
  }, 120000);

  els["input-images"].addEventListener("change", (e) => acceptImages([...e.target.files]));
  const zone = els["drop-images"];
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", async (e) => {
    e.preventDefault();
    zone.classList.remove("over");
    acceptImages(await filesFromDrop(e.dataTransfer));
  });
}

main();

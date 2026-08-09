# Dropzone Action Info
# Name: Convert to Markdown
# Description: Converts dropped documents to LLM-ready Markdown with doc2md. Output lands in ~/Downloads/doc2md. Hold Shift to stay fully local (no vision API calls). Hold Command to reveal the result in Finder.
# Handles: Files
# Creator: Nixon Sucuc
# URL: https://github.com/nixonsucuc/doc2md
# Events: Clicked, Dragged
# KeyModifiers: Command, Shift
# SkipConfig: Yes
# RunsSandboxed: No
# Version: 1.0
# MinDropzoneVersion: 4.0

import glob
import os
import re
import subprocess
import tempfile
from pathlib import Path

# Dropzone runs actions under its own bundled Python (Contents/Actions/lib/python),
# which knows nothing about doc2md's interpreter or its dependencies. So this
# action never imports doc2md — it shells out to the installed `doc2md` binary.

# Extensions doc2md actually converts. doc2md itself falls through to a plain-text
# read for anything it does not recognise, which quietly lands binary junk in the
# Markdown. A drop target is exactly where that happens, so the allow-list lives
# here: unknown types are reported as skipped instead of producing garbage.
SUPPORTED = {
    ".pdf", ".docx", ".pptx", ".epub", ".html", ".htm", ".csv", ".txt",
    ".json", ".ipynb", ".eml", ".msg", ".zip", ".xml", ".md",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif",
}

DEFAULT_OUTPUT_DIR = Path.home() / "Downloads" / "doc2md"
ENV_FILE = Path.home() / ".config" / "doc2md" / "env"

STEP_RE = re.compile(r"Step (\d)/6:\s*(.*)")
SAVED_RE = re.compile(r"^\s*Saved:\s*(.+?)\s*$")
# Counting the diagrams that needed the vision model. --no-vision is deliberately
# NOT passed when the key is missing: that flag also disables the step that
# decides an image is a diagram at all, so the count would always be zero and the
# document would convert silently thinner than it should be.
SEMANTIC_RE = re.compile(r"Analyzing (\d+) semantic image")
NO_KEY_RE = re.compile(r"GEMINI_API_KEY not set")


def find_doc2md():
    """Locate the installed doc2md executable.

    Dropzone launches from the GUI session, which has no shell PATH, so `which`
    is useless here. These are the locations pip and pipx actually use on macOS,
    newest Python version first.
    """
    candidates = []
    candidates += sorted(
        glob.glob("/Library/Frameworks/Python.framework/Versions/*/bin/doc2md"),
        reverse=True,
    )
    candidates += sorted(
        glob.glob(str(Path.home() / "Library/Python/*/bin/doc2md")), reverse=True
    )
    candidates += [
        str(Path.home() / ".local" / "bin" / "doc2md"),
        "/opt/homebrew/bin/doc2md",
        "/usr/local/bin/doc2md",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def build_env(binary):
    """Environment for the doc2md subprocess.

    Two things the GUI session does not provide: Homebrew's bin directory, without
    which pytesseract cannot find the tesseract binary and every OCR path fails;
    and GEMINI_API_KEY, which normally lives in a shell rc file that Dropzone
    never sources.
    """
    env = dict(os.environ)
    env["PATH"] = ":".join([
        os.path.dirname(binary),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ])

    if ENV_FILE.is_file():
        try:
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if value and not env.get(key):
                    env[key] = value
        except OSError:
            pass

    return env


def partition(paths):
    """Split dropped items into convertible files and skip reasons."""
    files, skipped = [], []
    for path in paths:
        if os.path.isdir(path):
            skipped.append((os.path.basename(path), "folder"))
        elif not os.path.isfile(path):
            skipped.append((os.path.basename(path), "not a file"))
        elif os.path.splitext(path)[1].lower() not in SUPPORTED:
            ext = os.path.splitext(path)[1].lower() or "no extension"
            skipped.append((os.path.basename(path), ext))
        else:
            files.append(path)
    return files, skipped



def convert_one(binary, path, env, no_vision, index, total):
    """Run doc2md on one file, mapping its progress onto the Dropzone bar.

    doc2md logs `Step N/6:` lines and a final `Saved: <path>` to stderr. Parsing
    them is more reliable than recomputing the output location, which depends on
    the naming rules inside doc2md.

    stdin and stdout are both kept off this process's own streams deliberately:
    Dropzone's API talks to the action over stdout and blocks on a stdin handshake
    after every message, so a child inheriting either would corrupt the protocol.
    stdout still has to be *kept*, though — doc2md exits 0 on an unreadable file
    and explains itself only in the report it prints there. It goes to a temp file
    rather than a second pipe, since nothing reads it until the process exits and
    a filled pipe buffer would deadlock.
    """
    command = [binary, path]
    if no_vision:
        command.append("--no-vision")

    report = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=report,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )

    name = os.path.basename(path)
    saved = None
    undescribed = 0
    pending_semantic = 0
    tail = []

    for line in process.stderr:
        line = line.rstrip("\n")
        tail.append(line)
        if len(tail) > 25:
            tail.pop(0)

        match = SAVED_RE.match(line)
        if match:
            saved = match.group(1)
            continue

        match = SEMANTIC_RE.search(line)
        if match:
            pending_semantic = int(match.group(1))
        elif NO_KEY_RE.search(line):
            undescribed += pending_semantic

        match = STEP_RE.search(line)
        if match:
            step, label = int(match.group(1)), match.group(2).rstrip(".")
            dz.percent(int(((index + step / 6.0) / total) * 100))
            prefix = "" if total == 1 else "[%d/%d] " % (index + 1, total)
            dz.begin("%s%s — %s" % (prefix, name, label))

    process.wait()

    if saved and os.path.isfile(saved):
        report.close()
        return saved, None, undescribed

    # Failed. doc2md puts its warnings in the report on stdout, so prefer those —
    # "Not a PDF: file is not a PDF" is worth showing; the step log is not.
    try:
        report.seek(0)
        warnings = [
            line.strip(" -\t")
            for line in report.read().splitlines()
            if line.lstrip().startswith("- ") or "failed" in line.lower()
        ]
    except (OSError, ValueError):
        warnings = []
    finally:
        report.close()

    detail = "\n".join(warnings) if warnings else "\n".join(
        line for line in tail if line.strip()
    )
    if process.returncode != 0:
        detail = detail or "doc2md exited with code %d" % process.returncode
    return None, detail or "doc2md produced no Markdown for this file.", 0



def dragged():
    binary = find_doc2md()
    if binary is None:
        dz.error(
            "doc2md not installed",
            "Could not find the doc2md command.\n\n"
            "Install it, then drop again:\n\n"
            "    cd ~/Developer/doc2md && pip3 install -e .",
        )

    files, skipped = partition(items)

    if not files:
        # dz.fail exits the action, so nothing runs after these.
        if skipped:
            dz.fail("Can't convert %s" % ", ".join(sorted({r for _, r in skipped})))
        dz.fail("Nothing to convert")

    modifiers = os.environ.get("KEY_MODIFIERS", "")
    reveal = "Command" in modifiers
    no_vision = "Shift" in modifiers

    env = build_env(binary)

    missing_key = not no_vision and not env.get("GEMINI_API_KEY")

    dz.begin("Converting %d file%s…" % (len(files), "" if len(files) == 1 else "s"))
    dz.determinate(True)
    dz.percent(0)

    outputs, failures, undescribed = [], [], 0
    for index, path in enumerate(files):
        output, error, skipped_images = convert_one(
            binary, path, env, no_vision, index, len(files)
        )
        if output:
            outputs.append(output)
            undescribed += skipped_images
        else:
            failures.append((os.path.basename(path), error))

    dz.percent(100)

    if not outputs:
        name, detail = failures[0]
        dz.error("Couldn't convert %s" % name, detail)

    # Summarise everything that did not go perfectly, so a partial run is never
    # silently reported as a clean success. Vision is only worth mentioning when
    # it was skipped *and* there was something it would have described.
    notes = []
    if failures:
        notes.append("%d failed" % len(failures))
    if skipped:
        notes.append("%d skipped" % len(skipped))
    if undescribed:
        notes.append(
            "%d diagram%s not described%s"
            % (
                undescribed,
                "" if undescribed == 1 else "s",
                " — no API key" if missing_key else "",
            )
        )
    suffix = " (%s)" % ", ".join(notes) if notes else ""

    # Command-drop reveals the result; otherwise the files just land in
    # ~/Downloads/doc2md and the notification is the whole feedback loop.
    if reveal:
        subprocess.call(["/usr/bin/open", "-R", outputs[0]])

    dz.finish(
        "Converted %d file%s%s"
        % (len(outputs), "" if len(outputs) == 1 else "s", suffix)
    )
    dz.url(False)


def clicked():
    folder = DEFAULT_OUTPUT_DIR
    if not folder.is_dir():
        dz.fail("No conversions yet")
        dz.url(False)
        return
    subprocess.call(["/usr/bin/open", str(folder)])
    dz.finish("Opened doc2md folder")
    dz.url(False)

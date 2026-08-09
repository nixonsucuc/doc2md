# Convert to Markdown — Dropzone action

Drag a document onto the Dropzone grid; it converts to Markdown in
`~/Downloads/doc2md`.

> **Requires Dropzone 4 Pro.** Custom Python actions are a Pro feature — without
> it this will not install. `../droplet/` is the free equivalent and needs no
> third-party app.

## Install

```bash
./dropzone/install.sh
```

It checks `doc2md`, `tesseract` and your API key, then hands the bundle to
Dropzone, which asks you which grid slot to use. Run with `--check` to see the
checks without installing.

## Use

| Gesture | Result |
|---|---|
| Drop file(s) | Converts, notification when done |
| **Shift** + drop | Fully local. No API calls, nothing leaves the machine. |
| **Command** + drop | Reveals the `.md` in Finder afterwards |
| Click the action | Opens `~/Downloads/doc2md` |

Files are written to `~/Downloads/doc2md/<name>/` exactly as the CLI does.
Dropping several files converts them in sequence under one progress bar.

## The API key

Dropzone launches from the GUI session and never sources your shell rc, so a
`GEMINI_API_KEY` exported in `.zshrc` is invisible to it. The action reads
`~/.config/doc2md/env` instead:

```
GEMINI_API_KEY=your-key-here
```

Without a key the action runs the local-only path automatically rather than
failing partway through a document. If diagrams were left undescribed as a
result, the completion notification says so and gives the count — silence means
nothing was lost.

## Notes on the implementation

- Dropzone runs actions under its own bundled Python 3.10, which knows nothing
  about doc2md's interpreter or dependencies. The action therefore shells out to
  the installed `doc2md` binary rather than importing it, and locates that binary
  by checking the paths pip and pipx use — `which` is useless with no shell PATH.
- `/opt/homebrew/bin` is put back on PATH for the subprocess. Without it
  pytesseract cannot find tesseract and every OCR path fails silently.
- Progress comes from parsing doc2md's `Step N/6:` lines on stderr, and the
  output location from its `Saved:` line.
- The child's stdin and stdout are kept off the action's own streams: Dropzone's
  API talks over stdout and blocks on a stdin handshake after every message.
  doc2md's stdout is still captured to a temp file, because it exits 0 on an
  unreadable file and explains itself only in the report it prints there.
- Unknown extensions are rejected here rather than passed through. doc2md falls
  back to a plain-text read for anything it does not recognise, which puts binary
  junk in the Markdown, and a drop target is the easy way to trigger that.

## Editing

`action.py` is plain Python. After changing it, click the Dropzone status item
and press **Cmd+R** to reload, or re-run `install.sh`. Test changes without
Dropzone using the harness pattern in the repo — inject `items` and a stub `dz`
as builtins, exactly as `Contents/Actions/lib/python_runner.py` does.

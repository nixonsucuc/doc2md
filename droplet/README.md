# doc2md.app — drag and drop

A droplet: drag documents onto its icon and they convert to Markdown in
`~/Downloads/doc2md`. Built from macOS's own tools, so it costs nothing and needs
nothing beyond doc2md.

## Build

```bash
./droplet/build.sh
```

Installs to `~/Applications/doc2md.app`. Drag it to the Dock — or to the Finder
toolbar, where it becomes a drop target on every window.

## Use

| | |
|---|---|
| Drop files on the icon | Converts them; progress bar while it runs, notification at the end |
| Double-click the app | Opens `~/Downloads/doc2md` |

If a document needs more than 20 diagram descriptions, the droplet converts it
first and then asks whether to spend the tokens, quoting the cost and what share
of your daily budget it is. Saying no still leaves you the converted document.
The notification reports tokens spent.

Unsupported files are counted as skipped. Failures show a dialog with the reason
from doc2md; successes stay quiet in Notification Center.

macOS will ask once for permission to send notifications, and once to control
Finder if you double-click the app. Both are expected.

## The API key

Apps launched from the Finder inherit nothing from `.zshrc`, so a key exported
there is invisible. Put it in `~/.config/doc2md/env` instead:

```
GEMINI_API_KEY=your-key-here
```

Without a key, conversions run local-only automatically rather than failing on
the first diagram.

## How it works

`droplet.applescript` is a thin loop — it exists to move the progress bar between
files and to turn exit codes into dialogs. The work is in `convert.sh`, bundled
into the app at `Contents/Resources/`:

- Finds the `doc2md` binary by checking where pip and pipx put it. `which` is
  useless in a GUI process with no shell PATH.
- Puts `/opt/homebrew/bin` back on PATH, without which pytesseract cannot find
  tesseract and every OCR path fails.
- Sources `~/.config/doc2md/env` with `set -a`, so a plain `KEY=value` line works
  without `export`.
- Decides success by doc2md's `Saved:` line, not its exit status — doc2md exits 0
  even on a file it could not read, and explains itself only in the report.
- Refuses unknown extensions. doc2md falls back to a plain-text read for anything
  it does not recognise, which puts binary junk in the Markdown.

Exit codes: `0` converted, `3` unsupported, `4` failed, `5` doc2md not installed.

`convert.sh` is usable on its own if you want to script it:

```bash
./droplet/convert.sh report.pdf
```

## Rebuilding

`build.sh` is destructive — it deletes and recreates the app. Re-run it after
editing either file. Editing the copy inside the `.app` works for a quick test
but is overwritten on the next build.

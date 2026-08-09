# doc2md Settings

A small window over the files doc2md already reads. Built with `swiftc` — no
Xcode project, no dependencies beyond the Command Line Tools.

```bash
./settings/build.sh
```

Installs `~/Applications/doc2md Settings.app`. Double-clicking the droplet opens
it.

## What it edits

| File | Holds |
|---|---|
| `~/.config/doc2md/config.json` | output folder, model, caps, daily budget, OCR languages |
| `~/.config/doc2md/env` | `GEMINI_API_KEY`, `chmod 600` |
| `~/.config/doc2md/usage.json` | read-only here — today's spend, shown as a progress bar |

The window owns no state. Editing these files by hand and using the window are
the same operation, which is why the CLI needs to know nothing about this app
and why the app can be deleted without consequence.

Secrets are kept out of `config.json` on purpose: preferences are the sort of
thing you paste into an issue when something misbehaves, and an API key is not.

## Notes on the implementation

- **Nothing here can reach the classifier.** `doc2md.CONFIGURABLE` is the
  allow-list, and the classification thresholds are absent from it. They were
  calibrated against a sample corpus, and a slider that silently degrades
  classification is worse than no slider. A test pins this.
- **The threshold can never exceed the cap.** Both the Swift side and
  `load_config()` clamp it, because a confirmation threshold above the hard cap
  can never fire — which would disable confirmation without saying so.
- **Writing the key preserves the file's comments** and re-applies `0600`
  afterwards. An atomic write replaces the inode and takes the old permissions
  with it, which would otherwise leave the key world-readable.
- **A corrupt config is not fatal** on either side: the app falls back to
  defaults, and so does `load_config()`. A broken preference must never cost you
  a conversion.
- **Ad-hoc signed** by `build.sh`. Unsigned SwiftUI apps are killed on launch on
  Apple silicon; ad-hoc is enough for a locally built app run by its author.
- `-parse-as-library` is required: with one source file, swiftc would otherwise
  treat it as a script with top-level code, which `@main` cannot coexist with.

## Adding a setting

1. Add it to `CONFIGURABLE` in `doc2md.py` with a parser and a validator.
2. Add a `@Published` field and a row in `Settings.swift`, and include it in the
   `payload` dictionary in `save()`.
3. Keep `Store.defaults` in step with the constant in `doc2md.py`.

The Swift defaults are a duplicate of the Python ones, which is the one piece of
duplication here. It buys the app the ability to run before doc2md is installed,
and to show sensible values when the config file does not exist yet.

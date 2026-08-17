# doc2md-ocr — Apple Vision OCR

An optional helper that routes OCR through Apple's Vision framework instead of
Tesseract. Without it nothing changes: doc2md uses Tesseract exactly as before.

```bash
./ocr/build.sh
```

Builds `~/.local/bin/doc2md-ocr` with `swiftc`. doc2md finds it there, in
`ocr/bin/`, or on the Homebrew/`/usr/local` paths.

> **Rebuild required as of the layout release.** The helper now also reports
> line geometry, which is what lets doc2md rebuild paragraphs, headings and lists
> instead of emitting one run-on block per page. A helper built before this
> ignores the new flag and prints plain lines; doc2md detects that and falls back
> to flat text rather than failing, so nothing breaks — you simply keep the old
> unstructured output until you re-run `./ocr/build.sh`.

## Output

```
doc2md-ocr page.png eng+spa            # one line of text per observation
doc2md-ocr page.png eng+spa --json     # one JSON object per observation
```

`--json` adds the bounding box and confidence:

```json
{"x":0.24045,"y":0.93304,"w":0.53483,"h":0.02336,"c":1.000,"t":"MAKE YOU LAUGH"}
```

Coordinates are normalised to the page, 0–1, origin bottom left — Vision's own
convention, passed through unconverted.

## Why

Measured against Tesseract on the sample corpus, at the same 200 dpi render:

| Source | Vision | Tesseract |
|---|---|---|
| Scanned book page | 1388 chars, 1.6 s | 1359 chars, 3.6 s |
| Brochure page | 1283 chars, 0.4 s | 1286 chars, 0.8 s |
| Concept map | 350 chars, 0.3 s | 248 chars, 0.4 s |
| Newspaper photo | **721 chars**, 0.4 s | **0 chars**, 0.1 s |

About twice as fast, never worse, and markedly better on degraded or
photographed sources — which is what it was built for, being a camera-input API
rather than a scanner one. The last row is the striking one: a 1998 Spanish
newspaper clipping that Tesseract read nothing at all from.

It also needs no `traineddata`: language support ships with the OS, so
`brew install tesseract` and the `spa.traineddata` dance become optional.

## Choosing the engine

`ocr_engine` in `~/.config/doc2md/config.json`, or the picker in the settings
window:

| Value | Behaviour |
|---|---|
| `auto` (default) | Vision when the helper is built, Tesseract otherwise. If Vision returns nothing, Tesseract gets a turn — an empty result is not an empty page. |
| `vision` | Vision only. No fallback, so a failure is visible rather than papered over. |
| `tesseract` | Tesseract only. The only option off macOS. |

Language codes stay in Tesseract's form (`eng+spa`); the helper maps them to
BCP-47 itself, so one setting covers both engines.

## Notes on the implementation

- **A separate binary, not PyObjC.** doc2md gains no Python dependency for a
  platform-specific optimisation, and the compiled artefact stays optional.
- **`usesLanguageCorrection` is off.** Vision's vocabulary correction helps prose
  and hurts part numbers, codes and tables. Documents have plenty of both.
- **`.accurate`, not `.fast`.** This runs once per page and fidelity is the point.
- **Failure is silent and non-zero.** The helper prints nothing and exits
  non-zero, so the caller falls back without parsing an error message.
- **No upscaling.** Vision does its own scaling; the upscale Tesseract needs
  below 300 dpi is neither required nor helpful here.
- **Observations come back in reading order, and nothing re-sorts them.** That
  order is column-aware: on a two-column page Vision finishes the left column
  before starting the right. Sorting by vertical position would therefore destroy
  the ordering rather than establish it, interleaving the columns line by line.
  The coordinates are for grouping decisions, not for deciding sequence — callers
  should treat them the same way.

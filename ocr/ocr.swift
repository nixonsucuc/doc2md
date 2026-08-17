// doc2md-ocr — Apple Vision text recognition, as a command-line filter.
//
//     doc2md-ocr <image> [lang,lang,…] [--json]
//
// Prints recognised text to stdout, one line per observation, and exits 0. With
// --json it prints one JSON object per line instead, carrying the bounding box
// and confidence alongside the text. On failure it prints nothing and exits
// non-zero, so the caller can fall back to Tesseract without parsing an error.
//
// The geometry is what lets the caller rebuild paragraphs, headings and lists:
// plain text alone collapses a whole page into one run-on block, because
// Markdown joins consecutive lines. Line height, the vertical gap to the
// previous line, and the left edge are between them enough to tell a heading
// from a paragraph break from a list item.
//
// Vision is on every Mac already: no Homebrew package, no traineddata files, no
// language downloads. It also reads photographed and skewed pages that Tesseract
// tends to lose, because it was built for camera input rather than scans.
//
// Exists as a separate binary rather than through PyObjC so that doc2md gains no
// Python dependency for a platform-specific optimisation, and so the compiled
// artefact stays optional — absent, the pipeline simply uses Tesseract.

import Foundation
import Vision
import AppKit

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(("doc2md-ocr: " + message + "\n").data(using: .utf8)!)
    exit(1)
}

/// Quote a string as a JSON scalar.
///
/// Hand-rolled rather than via JSONEncoder because encoding a bare string as a
/// top-level fragment needs an availability dance for one line of escaping, and
/// OCR output is plain recognised text — the only characters that can appear are
/// the ones handled here.
func encodeJSONString(_ value: String) -> String {
    var out = "\""
    for character in value.unicodeScalars {
        switch character {
        case "\"": out += "\\\""
        case "\\": out += "\\\\"
        case "\n": out += "\\n"
        case "\r": out += "\\r"
        case "\t": out += "\\t"
        default:
            if character.value < 0x20 {
                out += String(format: "\\u%04x", character.value)
            } else {
                out.unicodeScalars.append(character)
            }
        }
    }
    return out + "\""
}

var arguments = CommandLine.arguments
let wantsJSON = arguments.contains("--json")
arguments.removeAll { $0 == "--json" }
guard arguments.count >= 2 else { fail("usage: doc2md-ocr <image> [lang,lang,…] [--json]") }

let path = arguments[1]
// Tesseract-style codes in, BCP-47 out, so callers can keep using "eng+spa".
let languageMap = [
    "eng": "en-US", "spa": "es-ES", "fra": "fr-FR", "deu": "de-DE",
    "ita": "it-IT", "por": "pt-BR", "nld": "nl-NL", "chi_sim": "zh-Hans",
    "chi_tra": "zh-Hant", "jpn": "ja-JP", "kor": "ko-KR", "rus": "ru-RU",
]
var languages: [String] = ["en-US"]
if arguments.count >= 3 {
    let requested = arguments[2]
        .split(whereSeparator: { $0 == "," || $0 == "+" })
        .map(String.init)
        .map { languageMap[$0] ?? $0 }
    if !requested.isEmpty { languages = requested }
}

guard let image = NSImage(contentsOfFile: path),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil)
else { fail("could not read image at \(path)") }

let request = VNRecognizeTextRequest()
// .accurate over .fast: this runs once per page and the whole point is fidelity.
request.recognitionLevel = .accurate
request.recognitionLanguages = languages
// Vision's vocabulary correction helps prose and hurts codes, part numbers and
// tables. Documents contain plenty of both, so it stays off — the same reasoning
// that keeps Tesseract on its default dictionary behaviour.
request.usesLanguageCorrection = false

do {
    try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])
} catch {
    fail("recognition failed: \(error.localizedDescription)")
}

guard let observations = request.results else { exit(0) }

// Observations arrive in reading order already, and that order is column-aware:
// on a two-column page Vision finishes the left column before starting the
// right. Sorting by vertical position would therefore *destroy* the ordering
// rather than establish it, interleaving the columns line by line. Nothing here
// re-sorts, and callers should not either — the coordinates are for grouping
// decisions, not for deciding sequence.
var lines: [String] = []
for observation in observations {
    guard let candidate = observation.topCandidates(1).first else { continue }
    if wantsJSON {
        // Normalised coordinates, origin bottom-left, as Vision reports them.
        let box = observation.boundingBox
        let fields = [
            "\"x\":\(String(format: "%.5f", box.origin.x))",
            "\"y\":\(String(format: "%.5f", box.origin.y))",
            "\"w\":\(String(format: "%.5f", box.size.width))",
            "\"h\":\(String(format: "%.5f", box.size.height))",
            "\"c\":\(String(format: "%.3f", candidate.confidence))",
            "\"t\":\(encodeJSONString(candidate.string))",
        ]
        lines.append("{" + fields.joined(separator: ",") + "}")
    } else {
        lines.append(candidate.string)
    }
}
print(lines.joined(separator: "\n"))

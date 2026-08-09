// doc2md-ocr — Apple Vision text recognition, as a command-line filter.
//
//     doc2md-ocr <image> [lang,lang,…]
//
// Prints recognised text to stdout, one line per observation, and exits 0. On
// failure it prints nothing and exits non-zero, so the caller can fall back to
// Tesseract without having to parse an error.
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

let arguments = CommandLine.arguments
guard arguments.count >= 2 else { fail("usage: doc2md-ocr <image> [lang,lang,…]") }

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

// Observations arrive in reading order already, so no sorting is applied here.
// Reconstructing layout is the caller's problem, exactly as it is with Tesseract.
var lines: [String] = []
for observation in observations {
    if let candidate = observation.topCandidates(1).first {
        lines.append(candidate.string)
    }
}
print(lines.joined(separator: "\n"))

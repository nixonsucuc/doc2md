// doc2md Settings
//
// A small window over the two files doc2md already reads:
//
//   ~/.config/doc2md/config.json   preferences
//   ~/.config/doc2md/env           the API key, kept separate on purpose
//
// It deliberately owns no state of its own. Everything here is a view of those
// files, so editing them by hand and editing them here are the same operation,
// and the CLI needs to know nothing about this app.
//
// Only the settings doc2md marks configurable are exposed. The classifier
// thresholds are calibrated against a sample corpus and are not here on purpose:
// a slider that silently degrades classification is worse than no slider.

import SwiftUI

// MARK: - Store

/// Reads and writes the config and env files, with the same tolerance for
/// damage the Python side has: a broken file falls back to defaults rather than
/// refusing to open.
final class Store: ObservableObject {
    @Published var outputDir: String
    @Published var visionModel: String
    @Published var hardCap: Int
    @Published var warnThreshold: Int
    @Published var dailyBudget: Int
    @Published var ocrLanguages: String
    @Published var ocrEngine: String
    @Published var apiKey: String

    @Published var savedAt: Date?
    @Published var errorText: String?

    static let configURL = URL(fileURLWithPath: NSHomeDirectory())
        .appendingPathComponent(".config/doc2md/config.json")
    static let envURL = URL(fileURLWithPath: NSHomeDirectory())
        .appendingPathComponent(".config/doc2md/env")
    static let usageURL = URL(fileURLWithPath: NSHomeDirectory())
        .appendingPathComponent(".config/doc2md/usage.json")

    // Kept in step with the defaults in doc2md.py.
    private static let defaults: [String: Any] = [
        "output_dir": "\(NSHomeDirectory())/Downloads/doc2md",
        "vision_model": "gemini-3.6-flash",
        "vision_hard_cap": 50,
        "vision_warn_threshold": 20,
        "vision_daily_budget": 250_000,
        "ocr_languages": "eng+spa",
        "ocr_engine": "auto",
    ]

    init() {
        let raw = Store.readConfig()
        func str(_ k: String) -> String { (raw[k] as? String) ?? (Store.defaults[k] as! String) }
        func num(_ k: String) -> Int { (raw[k] as? Int) ?? (Store.defaults[k] as! Int) }

        outputDir = str("output_dir")
        visionModel = str("vision_model")
        hardCap = num("vision_hard_cap")
        warnThreshold = num("vision_warn_threshold")
        dailyBudget = num("vision_daily_budget")
        ocrLanguages = str("ocr_languages")
        ocrEngine = str("ocr_engine")
        apiKey = Store.readAPIKey()
    }

    private static func readConfig() -> [String: Any] {
        guard let data = try? Data(contentsOf: configURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return [:] }
        return obj
    }

    /// The key lives in a shell-style KEY=value file, because that is what the
    /// GUI front-ends source. Only the last uncommented assignment counts.
    private static func readAPIKey() -> String {
        guard let text = try? String(contentsOf: envURL, encoding: .utf8) else { return "" }
        var found = ""
        for line in text.components(separatedBy: .newlines) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard !trimmed.hasPrefix("#"), trimmed.hasPrefix("GEMINI_API_KEY=") else { continue }
            found = String(trimmed.dropFirst("GEMINI_API_KEY=".count))
                .trimmingCharacters(in: CharacterSet(charactersIn: "\"' "))
        }
        return found
    }

    /// Tokens spent today, or nil when the counter is absent or from an earlier
    /// day — the Python side resets on date change, so this mirrors that.
    var usedToday: Int? {
        guard let data = try? Data(contentsOf: Store.usageURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let date = obj["date"] as? String
        else { return nil }
        let today = ISO8601DateFormatter()
        today.formatOptions = [.withFullDate]
        guard date == today.string(from: Date()) else { return nil }
        return obj["tokens"] as? Int
    }

    func save() {
        errorText = nil
        // The threshold can never exceed the cap, or confirmation never fires.
        if warnThreshold > hardCap { warnThreshold = hardCap }

        let payload: [String: Any] = [
            "output_dir": outputDir,
            "vision_model": visionModel,
            "vision_hard_cap": hardCap,
            "vision_warn_threshold": warnThreshold,
            "vision_daily_budget": dailyBudget,
            "ocr_languages": ocrLanguages,
            "ocr_engine": ocrEngine,
        ]
        do {
            try FileManager.default.createDirectory(
                at: Store.configURL.deletingLastPathComponent(),
                withIntermediateDirectories: true)
            let data = try JSONSerialization.data(
                withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: Store.configURL, options: .atomic)
            try writeAPIKey()
            savedAt = Date()
        } catch {
            errorText = error.localizedDescription
        }
    }

    /// Rewrites only the key line, preserving the comments already in the file,
    /// and re-applies 0600 — an atomic write replaces the inode, taking the old
    /// permissions with it, which would otherwise leave the key world-readable.
    private func writeAPIKey() throws {
        let existing = (try? String(contentsOf: Store.envURL, encoding: .utf8)) ?? ""
        var kept = existing.components(separatedBy: .newlines).filter { line in
            let t = line.trimmingCharacters(in: .whitespaces)
            return t.hasPrefix("#") || (!t.hasPrefix("GEMINI_API_KEY=") && !t.isEmpty)
        }
        let key = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        if !key.isEmpty { kept.append("GEMINI_API_KEY=\(key)") }

        try (kept.joined(separator: "\n") + "\n")
            .write(to: Store.envURL, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: Store.envURL.path)
    }
}

// MARK: - View

struct SettingsView: View {
    @StateObject private var store = Store()
    @State private var showKey = false

    /// ~2,680 tokens per image, measured against gemini-3.6-flash including
    /// thinking tokens. Mirrors VISION_TOKENS_PER_IMAGE in doc2md.py.
    private let tokensPerImage = 2680

    var body: some View {
        Form {
            Section("Output") {
                HStack {
                    TextField("Folder", text: $store.outputDir)
                        .textFieldStyle(.roundedBorder)
                    Button("Choose…") { chooseFolder() }
                }
            }

            Section("Vision") {
                HStack {
                    if showKey {
                        TextField("Gemini API key", text: $store.apiKey)
                            .textFieldStyle(.roundedBorder)
                    } else {
                        SecureField("Gemini API key", text: $store.apiKey)
                            .textFieldStyle(.roundedBorder)
                    }
                    Button(showKey ? "Hide" : "Show") { showKey.toggle() }
                }
                Text(store.apiKey.isEmpty
                     ? "Without a key, documents still convert — diagrams are left undescribed."
                     : "Stored in ~/.config/doc2md/env, readable only by you.")
                    .font(.caption).foregroundStyle(.secondary)

                TextField("Model", text: $store.visionModel)
                    .textFieldStyle(.roundedBorder)

                Stepper("Confirm above \(store.warnThreshold) images",
                        value: $store.warnThreshold, in: 0...store.hardCap)
                Text("≈ \((store.warnThreshold * tokensPerImage).formatted()) tokens before you are asked.")
                    .font(.caption).foregroundStyle(.secondary)

                Stepper("Never exceed \(store.hardCap) images per document",
                        value: $store.hardCap, in: 1...500)
                Text("Worst case ≈ \((store.hardCap * tokensPerImage).formatted()) tokens, "
                     + "\(percent(store.hardCap * tokensPerImage))% of the daily budget.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            Section("Budget") {
                HStack {
                    Text("Daily token budget")
                    Spacer()
                    TextField("", value: $store.dailyBudget, format: .number)
                        .textFieldStyle(.roundedBorder).frame(width: 110)
                }
                if let used = store.usedToday {
                    ProgressView(value: min(Double(used) / Double(max(store.dailyBudget, 1)), 1.0))
                    Text("\(used.formatted()) of \(store.dailyBudget.formatted()) used today "
                         + "(\(percent(used))%). Resets at midnight.")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    Text("Nothing spent today.").font(.caption).foregroundStyle(.secondary)
                }
            }

            Section("OCR") {
                Picker("Engine", selection: $store.ocrEngine) {
                    Text("Automatic").tag("auto")
                    Text("Apple Vision").tag("vision")
                    Text("Tesseract").tag("tesseract")
                }
                Text("Automatic prefers Apple Vision when its helper is built "
                     + "(./ocr/build.sh) and falls back to Tesseract.")
                    .font(.caption).foregroundStyle(.secondary)

                TextField("Languages", text: $store.ocrLanguages)
                    .textFieldStyle(.roundedBorder)
                Text("Language codes joined by +, e.g. eng+spa. Vision maps these itself; "
                     + "Tesseract needs matching traineddata installed.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            if let error = store.errorText {
                Text(error).font(.caption).foregroundStyle(.red)
            }

            HStack {
                Text(store.savedAt == nil ? " " : "Saved. New conversions use these settings.")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Save") { store.save() }.keyboardShortcut(.defaultAction)
            }
        }
        .formStyle(.grouped)
        .frame(width: 460)
        .fixedSize(horizontal: false, vertical: true)
    }

    private func percent(_ tokens: Int) -> Int {
        Int((Double(tokens) / Double(max(store.dailyBudget, 1)) * 100).rounded())
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = URL(fileURLWithPath: store.outputDir)
        if panel.runModal() == .OK, let url = panel.url { store.outputDir = url.path }
    }
}

// MARK: - App

@main
struct DocToMdSettingsApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate

    var body: some Scene {
        Window("doc2md Settings", id: "settings") {
            SettingsView()
        }
        .windowResizability(.contentSize)
    }
}

/// Quitting on window close keeps this feeling like a settings sheet rather than
/// an app that lingers in the Dock after you are done with it.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }
}

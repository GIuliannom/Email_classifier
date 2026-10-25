import Foundation
import AppKit

// Mirrors GET /status from core/server.py
struct Status: Decodable {
    struct Summary: Decodable, Identifiable { let account: String; let text: String; let range_start: String; let kind: String; var id: String { account } }
    struct Proposal: Decodable, Identifiable { let id: Int; let name: String; let description: String?; let reason: String? }
    struct Event: Decodable, Identifiable { let id: Int; let title: String; let start: String; let end: String?; let location: String?; let reason: String?; let subject: String? }
    struct Settings: Decodable { let summary_time: String?; let images_auto: Bool? }
    let summaries: [Summary]
    let proposals: [Proposal]
    let events: [Event]
    let settings: Settings
    let busy: String?
    let errors: [String]
    let last_refresh: Double?
}

// Mirrors GET /mailboxes
struct Account: Decodable, Identifiable, Hashable {
    struct Section: Decodable, Hashable { let name: String; let total: Int; let unread: Int }
    struct Folders: Decodable, Hashable { let unread: Int; let starred: Int; let archive: Int; let junk: Int; let trash: Int }
    let id: Int
    let address: String
    let provider: String
    let sections: [Section]
    let folders: Folders
    let available: [String]   // sections this account uses, for "Move to"
    var unread: Int { folders.unread }
}
private struct Mailboxes: Decodable { let accounts: [Account] }

// Mirrors POST /ask: an answer from your own email, with the emails it used
struct AskEmail: Decodable, Identifiable, Hashable { let id: Int; let account: Int; let sender: String?; let subject: String?; let date: String? }
private struct AskResult: Decodable { let answer: String; let emails: [AskEmail]; let checked: Int }
struct ChatTurn: Identifiable {
    let id = UUID()
    let question: String
    var answer: String?
    var emails: [AskEmail] = []
    var checked = 0
    var failed = false
}

/// Talks to the Python engine on 127.0.0.1, starting it if it isn't running.
@MainActor
final class Engine: ObservableObject {
    @Published var status: Status?
    @Published var accounts: [Account] = []
    @Published var problem: String?
    @Published var openFolder: Folder?   // set by the menu bar: which folder the mail window should show
    @Published var chat: [ChatTurn] = []  // "ask your email", shared by the menu bar and the mail window
    @Published var showChat = false

    var asking: Bool { chat.last.map { $0.answer == nil } ?? false }

    /// Answered by the local model from your own email (core/ai/ask.py); nothing leaves the Mac
    func ask(_ question: String) async {
        let q = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty, !asking else { return }
        chat.append(ChatTurn(question: q))
        let i = chat.count - 1
        do {
            let r = try JSONDecoder().decode(AskResult.self, from: try await request("ask", body: ["question": q]))
            chat[i].answer = r.answer
            chat[i].emails = r.emails
            chat[i].checked = r.checked
        } catch {
            chat[i].answer = "Couldn't answer: \(error.localizedDescription)"
            chat[i].failed = true
        }
    }

    // The project folder; set EMAIL_AI_DIR to run it from somewhere else
    let projectDir = URL(fileURLWithPath: ProcessInfo.processInfo.environment["EMAIL_AI_DIR"]
                         ?? NSHomeDirectory() + "/Documents/email_LLM")
    private var server: Process?
    private var timer: Timer?

    init() {
        NotificationCenter.default.addObserver(forName: NSApplication.willTerminateNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.server?.terminate() }  // only stops an engine this app started
        }
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.reload() }
        }
        Task { await reload() }
    }

    private var token: String {
        (try? String(contentsOf: projectDir.appendingPathComponent("data/api_token"), encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    /// path may carry a query string, e.g. "messages?account=1&q=lunch"
    func request(_ path: String, body: [String: Any]? = nil) async throws -> Data {
        try await raw(path, body: body).0
    }

    /// Data plus the response (for attachments: the file name travels in X-Filename)
    func raw(_ path: String, body: [String: Any]? = nil) async throws -> (Data, HTTPURLResponse?) {
        guard let url = URL(string: "http://127.0.0.1:8765/" + path) else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.setValue(token, forHTTPHeaderField: "X-Token")
        req.timeoutInterval = 200  // UCF actions go through Apple Mail, which can be slow
        if let body {
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        let http = resp as? HTTPURLResponse
        if let http, http.statusCode >= 400, http.statusCode != 409 {
            let msg = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
            throw NSError(domain: "EmailAI", code: http.statusCode,
                          userInfo: [NSLocalizedDescriptionKey: msg ?? "HTTP \(http.statusCode)"])
        }
        return (data, http)
    }

    func get<T: Decodable>(_ path: String, as type: T.Type) async throws -> T {
        try JSONDecoder().decode(T.self, from: try await request(path))
    }

    /// POST and return the JSON reply as a dictionary
    @discardableResult
    func call(_ path: String, _ body: [String: Any] = [:]) async throws -> [String: Any] {
        (try JSONSerialization.jsonObject(with: try await request(path, body: body)) as? [String: Any]) ?? [:]
    }

    func reload() async {
        do {
            status = try JSONDecoder().decode(Status.self, from: try await request("status"))
            let fresh = try await get("mailboxes", as: Mailboxes.self).accounts
            if fresh != accounts { accounts = fresh }  // no needless sidebar redraw every 15 s
            problem = nil
        } catch let e as URLError where e.code == .cannotConnectToHost {
            startServer()
        } catch {
            problem = error.localizedDescription
        }
    }

    func post(_ path: String, _ body: [String: Any] = [:]) {
        Task {
            do { _ = try await request(path, body: body) } catch { problem = error.localizedDescription }
            await reload()
        }
    }

    private func startServer() {
        guard server == nil else { return }  // already starting; the next reload connects
        let p = Process()
        p.executableURL = projectDir.appendingPathComponent(".venv/bin/python")
        p.arguments = ["-m", "core.server"]
        p.currentDirectoryURL = projectDir
        let logURL = projectDir.appendingPathComponent("data/server.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        if let log = try? FileHandle(forWritingTo: logURL) {
            p.standardOutput = log
            p.standardError = log
        }
        // Only forget the engine if it's this one: an older engine's exit can arrive after a new start
        p.terminationHandler = { [weak self] ended in Task { @MainActor in if self?.server === ended { self?.server = nil } } }
        // Tells the engine to exit if this app goes away (quit, crash, force-quit): no orphan engines
        p.environment = ProcessInfo.processInfo.environment.merging(["EMAIL_AI_EXIT_WITH_PARENT": "1"]) { $1 }
        do {
            try p.run()
            server = p
            problem = "Starting the engine…"
            Task { try? await Task.sleep(nanoseconds: 3_000_000_000); await reload() }
        } catch {
            problem = "Couldn't start the engine: \(error.localizedDescription). Check \(projectDir.path)/.venv"
        }
    }
}

import SwiftUI
import WebKit
import UniformTypeIdentifiers

// Mirrors GET /messages and /message/<id> in core/server.py
struct MessageSummary: Decodable, Identifiable, Hashable {
    let id: Int; let account: Int; let sender: String?; let subject: String?
    let date: String?; let snippet: String; var unread: Bool; var section: String
    var starred: Bool; let status: String; let attachments: Bool
}
struct Attachment: Decodable, Hashable { let index: Int; let filename: String; let type: String; let size: Int }
struct MessageContent: Decodable {
    let id: Int
    let From: String?; let To: String?; let Cc: String?; let Date: String?; let Subject: String?
    let html: String?; let text: String?
    let attachments: [Attachment]?
    let account: Int?; let status: String?; let section: String?
    let sender_address: String?; var images_allowed: Bool?
}
private struct Messages: Decodable { let messages: [MessageSummary] }

/// What the sidebar selected
enum Folder: Hashable {
    case allInboxes, unread, starred, archive, junk, trash
    case inbox(Int)              // one account, every section
    case section(Int, String)    // one account, one section

    var query: String {
        switch self {
        case .allInboxes: return "folder=inbox"
        case .unread: return "folder=unread"
        case .starred: return "folder=starred"
        case .archive: return "folder=archive"
        case .junk: return "folder=junk"
        case .trash: return "folder=trash"
        case .inbox(let a): return "folder=inbox&account=\(a)"
        case .section(let a, let s): return "folder=inbox&account=\(a)&section=\(s.urlEncoded)"
        }
    }
    var account: Int? {
        switch self { case .inbox(let a), .section(let a, _): return a; default: return nil }
    }
    var title: String {
        switch self {
        case .allInboxes: return "All Inboxes"
        case .unread: return "Unread"
        case .starred: return "Starred"
        case .archive: return "Archive"
        case .junk: return "Junk"
        case .trash: return "Trash"
        case .inbox: return "Inbox"
        case .section(_, let s): return s
        }
    }
    /// Trash, Archive and Junk hold mail that left the inbox: their main action is bringing it back
    var isOutOfInbox: Bool { [.archive, .junk, .trash].contains(self) }
}

struct Toast: Identifiable { let id = UUID(); let text: String; let actionId: Int? }

@MainActor
final class MailStore: ObservableObject {
    @Published var messages: [MessageSummary] = []
    @Published var content: MessageContent?
    @Published var loadingContent = false
    @Published var contentError: String?
    @Published var toast: Toast?
    @Published var listError: String?
    private(set) var hasMore = false
    private var query = ""
    private var openingId: Int?
    let engine: Engine
    static let page = 100

    init(engine: Engine) { self.engine = engine }

    func load(_ folder: Folder, search: String) async {
        let q = search.trimmingCharacters(in: .whitespaces)
        var path = "messages?limit=\(Self.page)&" + (q.isEmpty ? folder.query : "q=\(q.urlEncoded)")
        if !q.isEmpty, let a = folder.account { path += "&account=\(a)" }  // search within the account you're in
        query = path
        do {
            let page = try await engine.get(path, as: Messages.self).messages
            guard query == path else { return }  // a newer search or folder replaced this one
            messages = page
            hasMore = page.count == Self.page
            listError = nil
        } catch { listError = error.localizedDescription }
    }

    func loadMore() async {
        guard hasMore else { return }
        hasMore = false
        let path = query + "&offset=\(messages.count)"
        if let next = try? await engine.get(path, as: Messages.self).messages {
            messages += next
            hasMore = next.count == Self.page
        }
    }

    func open(_ id: Int?) async {
        openingId = id
        content = nil
        contentError = nil
        guard let id else { return }
        loadingContent = true
        defer { if openingId == id { loadingContent = false } }
        do {
            let c = try await engine.get("message/\(id)", as: MessageContent.self)
            guard openingId == id else { return }  // you clicked another email meanwhile: never show a stale one
            content = c
        } catch {
            if openingId == id { contentError = error.localizedDescription }
            return
        }
        if let i = messages.firstIndex(where: { $0.id == id }), messages[i].unread {
            messages[i].unread = false  // instant; the account is told in the background
            Task { _ = try? await engine.request("message/\(id)/read", body: [:]); await engine.reload() }
        }
    }

    func summary(_ id: Int?) -> MessageSummary? { messages.first { $0.id == id } }

    /// Runs an action on one email. The list changes right away (UCF actions go through Apple Mail
    /// and can take a while); the account is told in the background, and a failure puts it back.
    /// Returns the id that should be selected next (or nil).
    func act(_ verb: String, on id: Int, in folder: Folder, body: [String: Any] = [:]) -> Int? {
        let label = ["trash": "Moved to Trash", "archive": "Archived", "junk": "Moved to Junk",
                     "restore": "Moved to Inbox", "move": "Moved to \(body["section"] as? String ?? "")",
                     "star": "Starred", "unstar": "Unstarred", "unread": "Marked as unread", "read": "Marked as read"][verb] ?? verb
        guard let i = messages.firstIndex(where: { $0.id == id }) else { return nil }
        let before = messages
        var leaves = ["trash", "archive", "junk", "restore"].contains(verb)
        switch verb {
        case "star", "unstar":
            messages[i].starred = verb == "star"
            leaves = folder == .starred && verb == "unstar"
        case "read", "unread": messages[i].unread = verb == "unread"
        case "move":
            let section = body["section"] as? String ?? ""
            messages[i].section = section
            if case .section(_, let s) = folder, s != section { leaves = true }
        default: break
        }
        var next: Int? = id
        if leaves {
            messages.remove(at: i)
            next = messages.indices.contains(i) ? messages[i].id : messages.last?.id  // like Mail: select the next one
        }
        Task {
            do {
                let result = try await engine.call("message/\(id)/\(verb)", body)
                toast = Toast(text: label, actionId: result["action_id"] as? Int)
            } catch {
                messages = before  // the account said no: show it the way it really is
                toast = Toast(text: "Couldn't do that: \(error.localizedDescription)", actionId: nil)
            }
            await engine.reload()
        }
        return next
    }

    func undo(_ toast: Toast, folder: Folder, search: String) async {
        guard let actionId = toast.actionId else { return }
        do { try await engine.call("undo", ["action_id": actionId]) } catch {
            self.toast = Toast(text: error.localizedDescription, actionId: nil); return
        }
        self.toast = Toast(text: "Undone", actionId: nil)
        await load(folder, search: search)
        await engine.reload()
    }

    func allowImages(from sender: String) async {
        _ = try? await engine.call("images/allow", ["sender": sender])
        content?.images_allowed = true
    }

    func openAttachment(_ a: Attachment, of id: Int) async {
        do {
            let (data, resp) = try await engine.raw("message/\(id)/attachment/\(a.index)")
            let name = (resp?.value(forHTTPHeaderField: "X-Filename")?.removingPercentEncoding) ?? a.filename
            let dir = FileManager.default.temporaryDirectory.appendingPathComponent("EmailAI-\(id)", isDirectory: true)
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let url = dir.appendingPathComponent((name as NSString).lastPathComponent)  // no paths from the email
            try data.write(to: url)
            NSWorkspace.shared.open(url)
        } catch { toast = Toast(text: "Couldn't open \(a.filename): \(error.localizedDescription)", actionId: nil) }
    }
}

struct ComposeRequest: Identifiable { let id = UUID(); let mode: String; let ref: Int?; let account: Int? }

struct MailWindow: View {
    @ObservedObject var engine: Engine
    @StateObject private var store: MailStore
    @State private var folder: Folder? = .allInboxes
    @State private var selection: Int?
    @State private var search = ""
    @State private var compose: ComposeRequest?
    @State private var addEvent: EventRequest?

    init(engine: Engine) {
        self.engine = engine
        _store = StateObject(wrappedValue: MailStore(engine: engine))
    }

    private struct LoadKey: Equatable { let folder: Folder?; let search: String }

    var body: some View {
        NavigationSplitView {
            Sidebar(engine: engine, folder: $folder)
                .navigationSplitViewColumnWidth(min: 210, ideal: 240)
        } content: {
            MessageList(store: store, selection: $selection, folder: folder ?? .allInboxes, accounts: engine.accounts,
                        act: { verb, id, body in run(verb, on: id, body: body) },
                        reply: { mode, id in compose = ComposeRequest(mode: mode, ref: id, account: nil) },
                        addEvent: { id in addEvent = EventRequest(id: id) })
                .id(folder)  // each folder opens at the top (newest), never mid-way from the last one
                .navigationTitle(search.isEmpty ? (folder ?? .allInboxes).title : "Search")
                .searchable(text: $search, placement: .toolbar, prompt: "Search all mail")
                .navigationSplitViewColumnWidth(min: 340, ideal: 400, max: 560)  // last, so it isn't wrapped away
        } detail: {
            ReaderPane(store: store, selectedId: selection, accounts: engine.accounts)
                .inspector(isPresented: $engine.showChat) {
                    ChatPanel(engine: engine) { id in Task { await store.open(id) } }
                        .inspectorColumnWidth(min: 300, ideal: 360, max: 520)
                }
        }
        .toolbar { toolbar }
        .sheet(item: $compose) { request in
            ComposeView(request: request, engine: engine) { sent in
                if sent { store.toast = Toast(text: "Sent", actionId: nil) }
            }
        }
        .sheet(item: $addEvent) { request in
            AddEventView(messageId: request.id, engine: engine) { actionId in
                if let actionId { store.toast = Toast(text: "Added to Calendar", actionId: actionId) }
            }
        }
        .overlay(alignment: .bottom) { toastView }
        .task(id: LoadKey(folder: folder, search: search)) {
            if !search.isEmpty { try? await Task.sleep(nanoseconds: 250_000_000) }  // wait until you pause typing
            guard !Task.isCancelled else { return }
            selection = nil
            await store.load(folder ?? .allInboxes, search: search)
        }
        .onChange(of: selection) { _, id in Task { await store.open(id) } }
        .onReceive(engine.$openFolder) { requested in
            if let requested { folder = requested; engine.openFolder = nil }
        }
        .frame(minWidth: 1000, minHeight: 600)
    }

    private func run(_ verb: String, on id: Int?, body: [String: Any] = [:]) {
        guard let id else { return }
        let next = store.act(verb, on: id, in: folder ?? .allInboxes, body: body)
        if next != id { selection = next }
    }

    /// The toolbar never changes shape: the same buttons stay in the same places and only enable or
    /// disable. Swapping or hiding items makes macOS rebuild the toolbar, which shifted the sidebar
    /// and list up and down (and could leave the newest emails tucked under the toolbar).
    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        let current = store.summary(selection)
        let none = selection == nil
        let out = (folder ?? .allInboxes).isOutOfInbox
        ToolbarItemGroup(placement: .navigation) {
            Button { compose = ComposeRequest(mode: "new", ref: nil, account: folder?.account) } label: {
                Label("New Message", systemImage: "square.and.pencil")
            }.keyboardShortcut("n").help("New Message (⌘N)")
        }
        ToolbarItemGroup {
            Button { compose = ComposeRequest(mode: "reply", ref: selection, account: nil) } label: {
                Label("Reply", systemImage: "arrowshape.turn.up.left")
            }.keyboardShortcut("r").help("Reply (⌘R)").disabled(none)
            Button { compose = ComposeRequest(mode: "replyall", ref: selection, account: nil) } label: {
                Label("Reply All", systemImage: "arrowshape.turn.up.left.2")
            }.keyboardShortcut("r", modifiers: [.command, .shift]).help("Reply All (⇧⌘R)").disabled(none)
            Button { compose = ComposeRequest(mode: "forward", ref: selection, account: nil) } label: {
                Label("Forward", systemImage: "arrowshape.turn.up.right")
            }.keyboardShortcut("f", modifiers: [.command, .shift]).help("Forward (⇧⌘F)").disabled(none)
        }
        ToolbarItemGroup {
            Button { run("archive", on: selection) } label: { Label("Archive", systemImage: "archivebox") }
                .keyboardShortcut("a", modifiers: [.command, .control]).help("Archive (⌃⌘A)").disabled(none || out)
            Button { run("junk", on: selection) } label: { Label("Junk", systemImage: "xmark.bin") }
                .keyboardShortcut("j", modifiers: [.command, .shift]).help("Move to Junk (⇧⌘J)").disabled(none || out)
            Button { run("trash", on: selection) } label: { Label("Delete", systemImage: "trash") }
                .keyboardShortcut(.delete, modifiers: .command).help("Move to Trash (⌘⌫)").disabled(none || folder == .trash)
            Button { run("restore", on: selection) } label: { Label("Move to Inbox", systemImage: "tray.and.arrow.up") }
                .help("Back to the Inbox (from Archive, Junk or Trash)").disabled(none || !out)
        }
        ToolbarItemGroup {
            Button { run(current?.starred == true ? "unstar" : "star", on: selection) } label: { Label("Star", systemImage: "star") }
                .keyboardShortcut("l", modifiers: [.command, .shift]).help("Star / Remove Star (⇧⌘L)").disabled(none)
            Button { run(current?.unread == true ? "read" : "unread", on: selection) } label: { Label("Unread", systemImage: "envelope.badge") }
                .keyboardShortcut("u", modifiers: [.command, .shift]).help("Mark as Read / Unread (⇧⌘U)").disabled(none)
            Menu {
                let available = engine.accounts.first { $0.id == current?.account }?.available ?? []
                ForEach(available, id: \.self) { s in Button(s) { run("move", on: selection, body: ["section": s]) } }
            } label: { Label("Move to", systemImage: "folder") }
            .help("Move to a section (teaches the sorter about this sender)").disabled(none)
            Button { if let id = selection { addEvent = EventRequest(id: id) } } label: {
                Label("Add to Calendar", systemImage: "calendar.badge.plus")
            }.keyboardShortcut("e", modifiers: [.command, .shift]).help("Add to Calendar (⇧⌘E)").disabled(none)
        }
        ToolbarItemGroup {
            Button { engine.showChat.toggle() } label: { Label("Ask", systemImage: "sparkles") }
                .keyboardShortcut("k").help("Ask your email (⌘K)")
            Button { Task { _ = try? await engine.call("refresh"); await store.load(folder ?? .allInboxes, search: search) } } label: {
                Label("Get Mail", systemImage: "arrow.clockwise")
            }.keyboardShortcut("r", modifiers: [.command, .option]).help("Get new mail (⌥⌘R)")
            Button { if let t = store.toast, t.actionId != nil { Task { await store.undo(t, folder: folder ?? .allInboxes, search: search) } } } label: {
                Label("Undo", systemImage: "arrow.uturn.backward")
            }.keyboardShortcut("z").help("Undo (⌘Z)").disabled(store.toast?.actionId == nil)
        }
    }

    @ViewBuilder private var toastView: some View {
        if let t = store.toast {
            HStack(spacing: 12) {
                Text(t.text).font(.callout)
                if t.actionId != nil {
                    Button("Undo") { Task { await store.undo(t, folder: folder ?? .allInboxes, search: search) } }
                        .buttonStyle(.link)
                }
            }
            .padding(.horizontal, 16).padding(.vertical, 10)
            .background(.regularMaterial, in: Capsule())
            .shadow(color: .black.opacity(0.15), radius: 8, y: 2)
            .padding(.bottom, 18)
            .transition(.move(edge: .bottom).combined(with: .opacity))
            .task(id: t.id) {
                try? await Task.sleep(nanoseconds: 6_000_000_000)
                if store.toast?.id == t.id { withAnimation(.easeOut(duration: 0.2)) { store.toast = nil } }
            }
        }
    }
}

private struct Sidebar: View {
    @ObservedObject var engine: Engine
    @Binding var folder: Folder?

    var body: some View {
        let totals = engine.accounts.reduce(into: (unread: 0, starred: 0, junk: 0)) {
            $0.unread += $1.folders.unread; $0.starred += $1.folders.starred; $0.junk += $1.folders.junk
        }
        List(selection: $folder) {
            Section("Mailboxes") {
                Label("All Inboxes", systemImage: "tray.2").badge(totals.unread).tag(Folder.allInboxes)
                Label("Unread", systemImage: "envelope.badge").badge(totals.unread).tag(Folder.unread)
                Label("Starred", systemImage: "star").badge(totals.starred).tag(Folder.starred)
            }
            ForEach(engine.accounts) { a in
                Section(accountName(a.address)) {
                    Label("Inbox", systemImage: "tray").badge(a.unread).tag(Folder.inbox(a.id))
                    ForEach(a.sections, id: \.name) { s in
                        Label(s.name, systemImage: sectionIcon(s.name)).badge(s.unread).tag(Folder.section(a.id, s.name))
                    }
                }
            }
            Section("Folders") {
                Label("Archive", systemImage: "archivebox").tag(Folder.archive)
                Label("Junk", systemImage: "xmark.bin").badge(totals.junk).tag(Folder.junk)
                Label("Trash", systemImage: "trash").tag(Folder.trash)
            }
        }
        .listStyle(.sidebar)
    }
}

private struct MessageList: View {
    @ObservedObject var store: MailStore
    @Binding var selection: Int?
    let folder: Folder
    let accounts: [Account]
    let act: (String, Int, [String: Any]) -> Void
    let reply: (String, Int) -> Void
    let addEvent: (Int) -> Void

    var body: some View {
        List(selection: $selection) {
            ForEach(store.messages) { m in
                MessageRow(message: m, showAccount: folder.account == nil && accounts.count > 1,
                           accountLabel: accountName(accounts.first { $0.id == m.account }?.address ?? ""))
                    .tag(m.id)
                    .contextMenu { menu(for: m) }
                    .onAppear { if m.id == store.messages.last?.id { Task { await store.loadMore() } } }
            }
        }
        .listStyle(.inset)
        .onDeleteCommand { if let id = selection, folder != .trash { act("trash", id, [:]) } }
        .overlay {
            if let e = store.listError {
                VStack(spacing: 8) {
                    Image(systemName: "exclamationmark.triangle").font(.title)
                    Text("Couldn't load mail").font(.headline)
                    Text(e).font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
                }.padding()
            } else if store.messages.isEmpty {
                Text("No Mail").font(.title3).foregroundStyle(.tertiary)
            }
        }
    }

    @ViewBuilder private func menu(for m: MessageSummary) -> some View {
        Button("Reply") { reply("reply", m.id) }
        Button("Reply All") { reply("replyall", m.id) }
        Button("Forward") { reply("forward", m.id) }
        Button("Add to Calendar…") { addEvent(m.id) }
        Divider()
        Button(m.unread ? "Mark as Read" : "Mark as Unread") { act(m.unread ? "read" : "unread", m.id, [:]) }
        Button(m.starred ? "Remove Star" : "Star") { act(m.starred ? "unstar" : "star", m.id, [:]) }
        Menu("Move to") {
            ForEach(accounts.first { $0.id == m.account }?.available ?? [], id: \.self) { s in
                Button(s) { act("move", m.id, ["section": s]) }
            }
        }
        Divider()
        if folder.isOutOfInbox {
            Button(folder == .junk ? "Not Junk" : "Move to Inbox") { act("restore", m.id, [:]) }
        } else {
            Button("Archive") { act("archive", m.id, [:]) }
            Button("Move to Junk") { act("junk", m.id, [:]) }
        }
        if folder != .trash { Button("Move to Trash", role: .destructive) { act("trash", m.id, [:]) } }
    }
}

private struct MessageRow: View {
    let message: MessageSummary
    let showAccount: Bool
    let accountLabel: String

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Circle().fill(message.unread ? Color.accentColor : .clear).frame(width: 8, height: 8).padding(.top, 5)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(displayName(message.sender)).fontWeight(message.unread ? .semibold : .regular).lineLimit(1)
                    Spacer(minLength: 4)
                    if message.attachments { Image(systemName: "paperclip").foregroundStyle(.secondary).imageScale(.small) }
                    Text(shortDate(message.date)).font(.caption).foregroundStyle(.secondary).monospacedDigit()
                }
                HStack(spacing: 4) {
                    Text(message.subject?.isEmpty == false ? message.subject! : "(no subject)")
                        .font(.callout).fontWeight(message.unread ? .medium : .regular).lineLimit(1)
                    if message.starred { Image(systemName: "star.fill").foregroundStyle(.yellow).imageScale(.small) }
                }
                Text(message.snippet).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                if showAccount || message.section != "Unsorted" {
                    HStack(spacing: 4) {
                        if showAccount { Chip(text: accountLabel, color: accountColor(accountLabel)) }
                        if message.section != "Unsorted" { Chip(text: message.section, color: .secondary) }
                    }
                }
            }
        }
        .padding(.vertical, 4)
    }
}

struct Chip: View {
    let text: String
    let color: Color
    var body: some View {
        Text(text).font(.caption2.weight(.medium)).foregroundStyle(color)
            .padding(.horizontal, 6).padding(.vertical, 1)
            .background(color.opacity(0.12), in: Capsule())
    }
}

struct ReaderPane: View {
    @ObservedObject var store: MailStore
    let selectedId: Int?
    let accounts: [Account]
    @State private var loadImages = false

    var body: some View {
        Group {
            if let c = store.content {
                reader(c)
            } else if let e = store.contentError {
                VStack(spacing: 10) {
                    Image(systemName: "envelope.badge.shield.half.filled").font(.largeTitle).foregroundStyle(.secondary)
                    Text("This email couldn't be opened").font(.headline)
                    Text(e).font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 360)
                    Button("Try Again") { Task { await store.open(selectedId) } }
                }
            } else if store.loadingContent {
                ProgressView()
            } else {
                Text(selectedId == nil ? "No Message Selected" : "").font(.title3).foregroundStyle(.tertiary)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onChange(of: selectedId) { loadImages = false }
    }

    private func reader(_ c: MessageContent) -> some View {
        let showImages = loadImages || (c.images_allowed ?? false)
        let summary = store.summary(c.id)
        return VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 10) {
                // At most 3 lines: an unbounded subject, measured by SwiftUI at a narrow trial width,
                // reported a huge height and pushed the whole window layout up and down per email
                Text(c.Subject?.isEmpty == false ? c.Subject! : "(no subject)")
                    .font(.title2.weight(.semibold)).textSelection(.enabled).lineLimit(3)
                HStack(alignment: .top, spacing: 10) {
                    Avatar(name: displayName(c.From))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(displayName(c.From)).font(.headline)
                        Text(c.sender_address ?? "").font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                        if let to = c.To, !to.isEmpty { Text("To: \(to)").font(.caption).foregroundStyle(.secondary).lineLimit(2) }
                        if let cc = c.Cc, !cc.isEmpty { Text("Cc: \(cc)").font(.caption).foregroundStyle(.secondary).lineLimit(2) }
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 4) {
                        Text(longDate(summary?.date) ?? c.Date ?? "").font(.caption).foregroundStyle(.secondary)
                        if let s = c.section, s != "Unsorted" { Chip(text: s, color: .accentColor) }
                    }
                }
                if !showImages, let html = c.html, hasRemoteImages(html) {
                    HStack(spacing: 8) {
                        Image(systemName: "eye.slash")
                        Text("Remote images are hidden so the sender can't tell you opened this.").font(.callout).lineLimit(2)
                        Spacer()
                        Button("Load Images") { loadImages = true }
                        if let address = c.sender_address, !address.isEmpty {
                            Button("Always from This Sender") { loadImages = true; Task { await store.allowImages(from: address) } }
                        }
                    }
                    .padding(8)
                    .background(.quaternary.opacity(0.6), in: RoundedRectangle(cornerRadius: 8))
                }
                if let atts = c.attachments, !atts.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(atts, id: \.index) { a in
                                Button { Task { await store.openAttachment(a, of: c.id) } } label: {
                                    HStack(spacing: 6) {
                                        Image(nsImage: NSWorkspace.shared.icon(for: .init(mimeType: a.type) ?? .data))
                                            .resizable().frame(width: 20, height: 20)
                                        VStack(alignment: .leading, spacing: 0) {
                                            Text(a.filename).font(.caption).lineLimit(1)
                                            Text(ByteCountFormatter.string(fromByteCount: Int64(a.size), countStyle: .file))
                                                .font(.caption2).foregroundStyle(.secondary)
                                        }
                                    }
                                    .padding(.horizontal, 8).padding(.vertical, 5)
                                    .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 7))
                                }
                                .buttonStyle(.plain)
                                .help("Open \(a.filename)")
                            }
                        }
                    }
                }
            }
            .padding(16)
            Divider()
            HTMLView(html: page(c), allowRemote: showImages)
        }
    }

    private func page(_ c: MessageContent) -> String {
        if let html = c.html { return html }
        let escaped = (c.text ?? "").replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;")
        return "<pre style=\"white-space:pre-wrap;font:14px -apple-system;margin:16px;color:#1d1d1f\">\(escaped)</pre>"
    }

    private func hasRemoteImages(_ html: String) -> Bool {
        html.range(of: #"(src|background)\s*=\s*["']?https?://"#, options: [.regularExpression, .caseInsensitive]) != nil
            || html.range(of: #"url\(\s*["']?https?://"#, options: [.regularExpression, .caseInsensitive]) != nil
    }
}

private struct Avatar: View {
    let name: String
    var body: some View {
        let initials = name.split(separator: " ").prefix(2).compactMap(\.first).map(String.init).joined().uppercased()
        Circle().fill(accountColor(name).gradient)
            .frame(width: 38, height: 38)
            .overlay(Text(initials.isEmpty ? "?" : initials).font(.system(size: 15, weight: .semibold)).foregroundStyle(.white))
    }
}

/// Shows email HTML safely: no JavaScript, remote content blocked unless allowed, links open in the
/// browser. Always drawn light: emails are designed for a white page.
private struct HTMLView: NSViewRepresentable {
    let html: String
    let allowRemote: Bool

    final class Coordinator: NSObject, WKNavigationDelegate {
        var loaded: (String, Bool)?
        var blocker: WKContentRuleList?
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
            if action.navigationType == .linkActivated, let url = action.request.url {
                NSWorkspace.shared.open(url)  // never browse inside the mail view
                decisionHandler(.cancel)
            } else {
                decisionHandler(.allow)
            }
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    /// Take exactly the space offered. Asked how big it wants to be, a web view answers with the
    /// whole email's height, and SwiftUI then stretched the window's layout past the window: the
    /// sidebar and list jumped up and down by a different amount for every email you opened.
    func sizeThatFits(_ proposal: ProposedViewSize, nsView: WKWebView, context: Context) -> CGSize? {
        CGSize(width: proposal.width ?? 400, height: proposal.height ?? 400)
    }

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.defaultWebpagePreferences.allowsContentJavaScript = false
        let view = WKWebView(frame: .zero, configuration: config)
        view.appearance = NSAppearance(named: .aqua)
        view.underPageBackgroundColor = .white
        view.navigationDelegate = context.coordinator
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        let c = context.coordinator
        guard c.loaded?.0 != html || c.loaded?.1 != allowRemote else { return }
        c.loaded = (html, allowRemote)
        let show = { _ = view.loadHTMLString(html, baseURL: nil) }
        view.configuration.userContentController.removeAllContentRuleLists()
        if allowRemote { show(); return }
        if let blocker = c.blocker {
            view.configuration.userContentController.add(blocker)
            show()
            return
        }
        // Block every http(s) load (tracking pixels, remote images and fonts) until you allow it
        let rules = #"[{"trigger":{"url-filter":"^https?://"},"action":{"type":"block"}}]"#
        WKContentRuleListStore.default().compileContentRuleList(forIdentifier: "block-remote", encodedContentRuleList: rules) { list, _ in
            if let list { c.blocker = list; view.configuration.userContentController.add(list) }
            show()
        }
    }
}

// MARK: - Formatting helpers

extension String {
    var urlEncoded: String { addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? self }
}

func displayName(_ sender: String?) -> String {
    guard let s = sender, !s.isEmpty else { return "(unknown)" }
    if let lt = s.firstIndex(of: "<") {
        let name = s[..<lt].trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: "\""))
        return name.isEmpty ? String(s[s.index(after: lt)...].dropLast()) : name
    }
    return s
}

/// "giulianno@gmail.com" -> "Gmail", "gi165093@ucf.edu" -> "UCF"
func accountName(_ address: String) -> String {
    let domain = address.split(separator: "@").last.map(String.init) ?? address
    if domain == "gmail.com" { return "Gmail" }
    return domain.split(separator: ".").first.map { $0.count <= 4 ? $0.uppercased() : $0.capitalized } ?? address
}

func accountColor(_ seed: String) -> Color {
    let palette: [Color] = [.blue, .purple, .pink, .orange, .teal, .indigo, .green, .red]
    return palette[abs(seed.unicodeScalars.reduce(0) { $0 &* 31 &+ Int($1.value) }) % palette.count]
}

func sectionIcon(_ section: String) -> String {
    ["News": "newspaper", "Banks": "banknote", "Personal": "person", "Social": "person.2",
     "Ads": "tag", "Updates": "bell", "Jobs": "briefcase", "School": "graduationcap", "Work": "building.2",
     "Sports": "sportscourt", "Unsorted": "tray"][section] ?? "folder"
}

private let isoParser = ISO8601DateFormatter()

private func parseDate(_ iso: String?) -> Date? {
    guard let iso else { return nil }
    return isoParser.date(from: iso + "Z")  // stored as naive UTC "YYYY-MM-DDTHH:MM:SS"
}

func shortDate(_ iso: String?) -> String {
    guard let d = parseDate(iso) else { return "" }
    let cal = Calendar.current
    if cal.isDateInToday(d) { return d.formatted(date: .omitted, time: .shortened) }
    if cal.isDateInYesterday(d) { return "Yesterday" }
    if let days = cal.dateComponents([.day], from: d, to: Date()).day, days < 7 { return d.formatted(.dateTime.weekday(.wide)) }
    return d.formatted(date: .numeric, time: .omitted)
}

func longDate(_ iso: String?) -> String? {
    parseDate(iso).map { $0.formatted(date: .abbreviated, time: .shortened) }
}

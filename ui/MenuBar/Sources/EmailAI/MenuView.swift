import SwiftUI

/// Height of the scrolling content, so the menu is only as tall as it needs to be
private struct ContentHeight: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

/// The menu-bar window: how your mail stands at a glance, what needs your answer, the summary.
struct ContentView: View {
    @EnvironmentObject var engine: Engine
    @Environment(\.openWindow) private var openWindow
    @State private var summaryTime = Date()
    @State private var contentHeight: CGFloat = 300
    @State private var summaryAccount = ""

    // A menu-bar window can't grow past the screen: the middle scrolls, header and footer stay put
    private var maxScrollHeight: CGFloat { (NSScreen.main?.visibleFrame.height ?? 800) * 0.7 }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let problem = engine.problem { notice(problem, color: .orange) }
                    ForEach(engine.status?.errors ?? [], id: \.self) { notice($0, color: .red) }
                    accounts
                    quickAsk
                    if let s = engine.status {
                        questions(s)
                        summary(s)
                    }
                }
                .padding(.trailing, 6)  // room for the scroll bar
                .background(GeometryReader { Color.clear.preference(key: ContentHeight.self, value: $0.size.height) })
            }
            .frame(height: min(contentHeight, maxScrollHeight))
            .onPreferenceChange(ContentHeight.self) { contentHeight = $0 }
            footer
        }
        .padding(16)
        .frame(width: 380)
        .task { await engine.reload() }
    }

    private func openMail(_ folder: Folder? = nil) {
        engine.openFolder = folder
        openWindow(id: "mail")
        NSApp.activate(ignoringOtherApps: true)
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "envelope.fill")
                .font(.system(size: 15, weight: .semibold)).foregroundStyle(.white)
                .frame(width: 30, height: 30)
                .background(LinearGradient(colors: [.blue, .indigo], startPoint: .top, endPoint: .bottom),
                            in: RoundedRectangle(cornerRadius: 8))
            VStack(alignment: .leading, spacing: 1) {
                Text("Email AI").font(.headline)
                statusLine.font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button { engine.post("refresh") } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.borderless).help("Get new mail now")
                .disabled(engine.status?.busy != nil)
        }
    }

    @ViewBuilder private var statusLine: some View {
        if let busy = engine.status?.busy {
            HStack(spacing: 4) { ProgressView().controlSize(.mini); Text(busy.capitalized + "…") }
        } else if let t = engine.status?.last_refresh, t > 0 {
            Text("Updated \(Date(timeIntervalSince1970: t).formatted(.relative(presentation: .named)))")
        } else {
            Text("Starting…")
        }
    }

    private func notice(_ text: String, color: Color) -> some View {
        Label(text, systemImage: "exclamationmark.triangle.fill")
            .font(.caption).foregroundStyle(color).lineLimit(3)
            .padding(8).frame(maxWidth: .infinity, alignment: .leading)
            .background(color.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))
    }

    private var accounts: some View {
        VStack(spacing: 6) {
            ForEach(engine.accounts) { a in
                Button { openMail(.inbox(a.id)) } label: {
                    HStack(spacing: 10) {
                        Circle().fill(accountColor(accountName(a.address)).gradient).frame(width: 26, height: 26)
                            .overlay(Text(String(accountName(a.address).prefix(1))).font(.caption.bold()).foregroundStyle(.white))
                        VStack(alignment: .leading, spacing: 0) {
                            Text(accountName(a.address)).font(.subheadline.weight(.semibold))
                            Text(a.address).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                        Spacer()
                        if a.unread > 0 {
                            Text("\(a.unread)").font(.caption.weight(.semibold)).monospacedDigit()
                                .padding(.horizontal, 7).padding(.vertical, 2)
                                .background(Color.accentColor.opacity(0.15), in: Capsule())
                                .foregroundStyle(Color.accentColor)
                        }
                        Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
                    }
                    .padding(8)
                    .contentShape(Rectangle())
                    .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10))
                }
                .buttonStyle(.plain)
                .help("Open \(a.address)")
            }
        }
    }

    @State private var askDraft = ""

    /// Ask your email from the menu bar; the full conversation is in the mail window's Ask panel
    private var quickAsk: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "sparkles").foregroundStyle(.purple)
                TextField("Ask your email…", text: $askDraft)
                    .textFieldStyle(.plain)
                    .onSubmit { let q = askDraft; askDraft = ""; Task { await engine.ask(q) } }
                    .disabled(engine.asking)
            }
            .padding(8)
            .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10))
            if let last = engine.chat.last {
                TurnView(turn: last, accounts: engine.accounts, openEmail: { _ in }, compact: true)
                if last.answer != nil {
                    Button("Open in Mail") { engine.showChat = true; openMail() }
                        .buttonStyle(.link).font(.caption)
                }
            }
        }
    }

    @ViewBuilder private func questions(_ s: Status) -> some View {
        if !s.proposals.isEmpty || !s.events.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Text("Waiting for you").font(.caption.weight(.semibold)).foregroundStyle(.secondary).textCase(.uppercase)
                ForEach(s.proposals) { p in
                    card(icon: "folder.badge.plus", title: "New section: \(p.name)",
                         detail: [p.description, p.reason].compactMap { $0 }.joined(separator: "\n"),
                         yes: "Create", no: "No") { engine.post("proposals/\(p.id)/\($0 ? "accept" : "reject")") }
                }
                ForEach(s.events) { e in
                    card(icon: "calendar.badge.plus", title: e.title,
                         detail: "\(e.start)\(e.location.map { $0.isEmpty ? "" : " · \($0)" } ?? "")\nWhy asking: \(e.reason ?? "")",
                         yes: "Add to Calendar", no: "Skip") { engine.post("events/\(e.id)/\($0 ? "accept" : "reject")") }
                }
            }
        }
    }

    private func card(icon: String, title: String, detail: String, yes: String, no: String,
                      answer: @escaping (Bool) -> Void) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon).foregroundStyle(Color.accentColor).frame(width: 18)
            VStack(alignment: .leading, spacing: 5) {
                Text(title).font(.subheadline.weight(.semibold))
                Text(detail).font(.caption).foregroundStyle(.secondary).lineLimit(4)
                HStack {
                    Button(yes) { answer(true) }.buttonStyle(.borderedProminent).controlSize(.small)
                    Button(no) { answer(false) }.controlSize(.small)
                }
            }
        }
        .padding(10).frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10))
    }

    @ViewBuilder private func summary(_ s: Status) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Summary").font(.caption.weight(.semibold)).foregroundStyle(.secondary).textCase(.uppercase)
                Spacer()
                Button("Summarize Now") { engine.post("summarize") }
                    .buttonStyle(.borderless).font(.caption).disabled(s.busy != nil)
            }
            if s.summaries.isEmpty {
                Text("No summary yet. It's written at your summary time, or press Summarize Now.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                let ordered = s.summaries.sorted { a, b in  // same order as the accounts above
                    (engine.accounts.firstIndex { $0.address == a.account } ?? 99) < (engine.accounts.firstIndex { $0.address == b.account } ?? 99)
                }
                if ordered.count > 1 {
                    Picker("", selection: $summaryAccount) {
                        ForEach(ordered) { Text(accountName($0.account)).tag($0.account) }
                    }
                    .pickerStyle(.segmented).labelsHidden()
                }
                let shown = ordered.first { $0.account == summaryAccount } ?? ordered[0]
                ScrollView {
                    Text(shown.text).font(.callout).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 240)
                .padding(10)
                .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10))
                .onAppear { if summaryAccount.isEmpty { summaryAccount = ordered[0].account } }
            }
        }
    }

    private var footer: some View {
        VStack(spacing: 10) {
            Button { openMail() } label: {
                Label("Open Mail", systemImage: "tray.full").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent).controlSize(.large)
            .keyboardShortcut("o")
            HStack {
                Image(systemName: "clock").foregroundStyle(.secondary)
                Text("Daily summary").font(.caption).foregroundStyle(.secondary)
                DatePicker("", selection: $summaryTime, displayedComponents: .hourAndMinute).labelsHidden()
                    .onChange(of: summaryTime) { _, new in
                        let t = Self.formatter.string(from: new)
                        if t != engine.status?.settings.summary_time { engine.post("settings", ["summary_time": t]) }
                    }
                Spacer()
                Button("Quit") { NSApplication.shared.terminate(nil) }.buttonStyle(.borderless).font(.caption)
            }
            Toggle(isOn: Binding(get: { engine.status?.settings.images_auto ?? true },
                                 set: { engine.post("settings", ["images_auto": $0]) })) {
                Text("Load remote images").font(.caption)
            }
            .toggleStyle(.switch).controlSize(.mini)
            .help("Off: images stay hidden until you load them, so senders can't tell when you open their email")
            .onAppear { summaryTime = Self.date(from: engine.status?.settings.summary_time ?? "21:00") }
        }
    }

    private static let formatter: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "HH:mm"; f.locale = Locale(identifier: "en_US_POSIX"); return f
    }()

    private static func date(from hhmm: String) -> Date { formatter.date(from: hhmm) ?? Date() }
}

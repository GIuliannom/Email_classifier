import SwiftUI

/// "Ask your email": questions answered by the local model from your own mail.
/// Answers link to the emails they came from; clicking one opens it in the reader.
struct ChatPanel: View {
    @ObservedObject var engine: Engine
    let openEmail: (Int) -> Void
    @State private var draft = ""
    @FocusState private var focused: Bool

    static let suggestions = [
        "Which students said they can't come to class this week?",
        "What's new in tech today?",
        "Anything I need to reply to today?",
        "Any deadlines this week?",
    ]

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Label("Ask Your Email", systemImage: "sparkles").font(.headline)
                Spacer()
                if !engine.chat.isEmpty {
                    Button("Clear") { engine.chat.removeAll() }.buttonStyle(.borderless).disabled(engine.asking)
                }
            }
            .padding(12)
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if engine.chat.isEmpty { intro }
                        ForEach(engine.chat) { turn in
                            TurnView(turn: turn, accounts: engine.accounts, openEmail: openEmail).id(turn.id)
                        }
                    }
                    .padding(12)
                }
                .onChange(of: engine.chat.count) { _, _ in
                    if let last = engine.chat.last { withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(last.id, anchor: .bottom) } }
                }
            }
            Divider()
            HStack(spacing: 8) {
                TextField("Ask about your email…", text: $draft, axis: .vertical)
                    .textFieldStyle(.plain).lineLimit(1...4).focused($focused)
                    .onSubmit(send)
                Button(action: send) { Image(systemName: "arrow.up.circle.fill").font(.title2) }
                    .buttonStyle(.borderless)
                    .disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty || engine.asking)
                    .keyboardShortcut(.return, modifiers: .command)
                    .help("Ask (↩)")
            }
            .padding(10)
            .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10))
            .padding(10)
        }
        .onAppear { focused = true }
    }

    private var intro: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Ask anything about your mail. The answer is written on your Mac from your own emails, and links to the ones it used.")
                .font(.callout).foregroundStyle(.secondary)
            ForEach(Self.suggestions, id: \.self) { s in
                Button { draft = s; send() } label: {
                    Text(s).font(.callout).multilineTextAlignment(.leading)
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(.plain)
            }
        }
    }

    private func send() {
        let q = draft
        draft = ""
        Task { await engine.ask(q) }
    }
}

struct TurnView: View {
    let turn: ChatTurn
    let accounts: [Account]
    let openEmail: (Int) -> Void
    var compact = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Spacer(minLength: 30)
                Text(turn.question).font(.callout).foregroundStyle(.white)
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(Color.accentColor, in: RoundedRectangle(cornerRadius: 12))
            }
            if let answer = turn.answer {
                Text(answer).font(.callout).textSelection(.enabled)
                    .foregroundStyle(turn.failed ? .red : .primary)
                    .lineLimit(compact ? 6 : nil)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if !turn.emails.isEmpty && !compact {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(turn.emails) { e in
                            Button { openEmail(e.id) } label: {
                                HStack(spacing: 6) {
                                    Image(systemName: "envelope").foregroundStyle(.secondary)
                                    VStack(alignment: .leading, spacing: 0) {
                                        Text(displayName(e.sender)).font(.caption.weight(.semibold)).lineLimit(1)
                                        Text(e.subject ?? "").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                    }
                                    Spacer()
                                    Text(shortDate(e.date)).font(.caption2).foregroundStyle(.secondary)
                                }
                                .padding(6)
                                .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 7))
                            }
                            .buttonStyle(.plain)
                            .help("Open this email")
                        }
                    }
                }
                if turn.checked > 0 {
                    Text("\(turn.emails.count) of \(turn.checked) emails used").font(.caption2).foregroundStyle(.tertiary)
                }
            } else {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("Reading your email…").font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }
}

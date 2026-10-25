import SwiftUI

/// New message / reply / reply all / forward, for any account (UCF sends through Apple Mail).
/// Nothing is sent until you press Send; AI drafts only fill in text for you to read and edit.
struct ComposeView: View {
    let request: ComposeRequest
    @ObservedObject var engine: Engine
    let done: (Bool) -> Void
    @Environment(\.dismiss) private var dismiss

    @State private var account: Int = 0
    @State private var to = ""
    @State private var cc = ""
    @State private var subject = ""
    @State private var text = ""          // what you write
    @State private var quoted = ""        // the original, below your text
    @State private var instructions = ""
    @State private var busy: String?
    @State private var error: String?
    @FocusState private var focus: Field?
    private enum Field { case to, body }

    private var isReply: Bool { request.mode != "new" }
    private var title: String {
        ["reply": "Reply", "replyall": "Reply All", "forward": "Forward"][request.mode] ?? "New Message"
    }

    var body: some View {
        VStack(spacing: 0) {
            Form {
                Picker("From", selection: $account) {
                    ForEach(engine.accounts) { a in Text(a.address).tag(a.id) }
                }
                .disabled(isReply)  // replies go out from the account that received the email
                TextField("To", text: $to, prompt: Text("name@example.com, …")).focused($focus, equals: .to)
                TextField("Cc", text: $cc)
                TextField("Subject", text: $subject)
            }
            .formStyle(.grouped)
            .frame(height: 200)

            if isReply && request.mode != "forward" {
                HStack(spacing: 8) {
                    Image(systemName: "sparkles").foregroundStyle(.purple)
                    TextField("What should the reply say? (optional)", text: $instructions)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { draft() }
                    Button("Draft with AI", action: draft).disabled(busy != nil)
                }
                .padding(.horizontal, 20).padding(.bottom, 8)
                .help("The local model writes a draft. Nothing is sent until you press Send.")
            }

            TextEditor(text: $text)
                .font(.body)
                .focused($focus, equals: .body)
                .padding(8)
                .background(Color(nsColor: .textBackgroundColor))
                .overlay(alignment: .topLeading) {
                    if text.isEmpty { Text("Write your message…").foregroundStyle(.tertiary).padding(.horizontal, 13).padding(.vertical, 8).allowsHitTesting(false) }
                }
            if !quoted.isEmpty {
                DisclosureGroup("Original message") {
                    ScrollView { Text(quoted).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled) }
                        .frame(maxHeight: 120)
                }
                .padding(.horizontal, 20).padding(.vertical, 6)
            }
            Divider()
            HStack {
                if let busy { ProgressView().controlSize(.small); Text(busy).font(.caption).foregroundStyle(.secondary) }
                if let error { Label(error, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.red).lineLimit(2) }
                Spacer()
                Button("Cancel", role: .cancel) { done(false); dismiss() }.keyboardShortcut(.cancelAction)
                Button("Send", action: send)
                    .keyboardShortcut(.return, modifiers: .command)
                    .buttonStyle(.borderedProminent)
                    .disabled(busy != nil || to.trimmingCharacters(in: .whitespaces).isEmpty)
                    .help("Send (⌘↩). It can't be unsent.")
            }
            .padding(14)
        }
        .frame(minWidth: 620, minHeight: 560)
        .navigationTitle(title)
        .task { await prefill() }
    }

    private func prefill() async {
        account = request.account ?? engine.accounts.first?.id ?? 0
        guard let ref = request.ref, isReply else { focus = .to; return }
        busy = "Loading…"
        defer { busy = nil }
        do {
            let data = try await engine.request("message/\(ref)/compose?mode=\(request.mode)")
            let f = (try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
            account = f["account"] as? Int ?? account
            to = f["to"] as? String ?? ""
            cc = f["cc"] as? String ?? ""
            subject = f["subject"] as? String ?? ""
            quoted = (f["body"] as? String ?? "").trimmingCharacters(in: .newlines)
            focus = request.mode == "forward" ? .to : .body
        } catch { self.error = error.localizedDescription }
    }

    private func draft() {
        guard let ref = request.ref else { return }
        Task {
            busy = "Drafting with the local model…"
            defer { busy = nil }
            do {
                let r = try await engine.call("draft", ["message_id": ref, "instructions": instructions])
                text = (r["text"] as? String ?? "") + (text.isEmpty ? "" : "\n\n" + text)
                error = nil
            } catch { self.error = error.localizedDescription }
        }
    }

    private func send() {
        Task {
            busy = "Sending…"
            defer { busy = nil }
            var body: [String: Any] = ["account": account, "mode": request.mode, "to": to, "cc": cc,
                                       "subject": subject, "body": text + (quoted.isEmpty ? "" : "\n\n" + quoted)]
            if let ref = request.ref { body["ref"] = ref }
            do {
                try await engine.call("send", body)
                done(true)
                dismiss()
            } catch { self.error = error.localizedDescription }
        }
    }
}

import SwiftUI

struct EventRequest: Identifiable { let id: Int }   // the email to make an event from

/// Add to Calendar: the local model reads the email and fills this in; you check it and press Add.
struct AddEventView: View {
    let messageId: Int
    @ObservedObject var engine: Engine
    let done: (Int?) -> Void   // the action id, for Undo
    @Environment(\.dismiss) private var dismiss

    @State private var title = ""
    @State private var start = Date()
    @State private var end = Date().addingTimeInterval(3600)
    @State private var allDay = false
    @State private var location = ""
    @State private var busy: String? = "Reading the email…"
    @State private var error: String?
    @State private var noEventFound = false

    private static let timed: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd'T'HH:mm"; f.locale = Locale(identifier: "en_US_POSIX"); return f
    }()
    private static let day: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; f.locale = Locale(identifier: "en_US_POSIX"); return f
    }()

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Label("Add to Calendar", systemImage: "calendar.badge.plus").font(.headline).padding(16)
            if noEventFound {
                Text("No date was found in this email; fill it in yourself.")
                    .font(.caption).foregroundStyle(.secondary).padding(.horizontal, 16)
            }
            Form {
                TextField("Title", text: $title)
                Toggle("All day", isOn: $allDay)
                DatePicker("Starts", selection: $start, displayedComponents: allDay ? [.date] : [.date, .hourAndMinute])
                if !allDay {
                    DatePicker("Ends", selection: $end, in: start..., displayedComponents: [.date, .hourAndMinute])
                }
                TextField("Location", text: $location)
            }
            .formStyle(.grouped)
            .disabled(busy != nil)
            HStack {
                if let busy { ProgressView().controlSize(.small); Text(busy).font(.caption).foregroundStyle(.secondary) }
                if let error { Label(error, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.red).lineLimit(2) }
                Spacer()
                Button("Cancel", role: .cancel) { done(nil); dismiss() }.keyboardShortcut(.cancelAction)
                Button("Add", action: add).keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent)
                    .disabled(busy != nil || title.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .padding(16)
        }
        .frame(width: 440)
        .task { await prefill() }
    }

    private func prefill() async {
        defer { busy = nil }
        do {
            let data = try await engine.request("message/\(messageId)/event")
            let ev = (try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
            title = ev["title"] as? String ?? ""
            location = ev["location"] as? String ?? ""
            let s = ev["start"] as? String ?? ""
            if let d = Self.timed.date(from: s) {
                start = d
                end = Self.timed.date(from: ev["end"] as? String ?? "") ?? d.addingTimeInterval(3600)
            } else if let d = Self.day.date(from: s) {
                start = d; allDay = true
            }
            noEventFound = (ev["has_event"] as? Bool) != true && s.isEmpty
        } catch { self.error = error.localizedDescription }
    }

    private func add() {
        Task {
            busy = "Adding…"
            defer { busy = nil }
            let body: [String: Any] = [
                "title": title, "location": location,
                "start": allDay ? Self.day.string(from: start) : Self.timed.string(from: start),
                "end": allDay ? "" : Self.timed.string(from: max(end, start)),
            ]
            do {
                let r = try await engine.call("message/\(messageId)/event", body)
                done(r["action_id"] as? Int)
                dismiss()
            } catch { self.error = error.localizedDescription }
        }
    }
}

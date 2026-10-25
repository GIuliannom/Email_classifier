import SwiftUI

@main
struct EmailAIApp: App {
    @StateObject private var engine = Engine()

    var body: some Scene {
        MenuBarExtra {
            ContentView().environmentObject(engine)
        } label: {
            // Badge the icon when something is waiting for your answer
            let questions = (engine.status?.proposals.count ?? 0) + (engine.status?.events.count ?? 0)
            Image(systemName: questions > 0 ? "envelope.badge" : "envelope")
        }
        .menuBarExtraStyle(.window)

        Window("Email AI", id: "mail") {
            MailWindow(engine: engine)
                // A menu-bar app has no Dock icon; give it one (and Cmd-Tab) while the mail window is open
                .onAppear { NSApp.setActivationPolicy(.regular); NSApp.activate(ignoringOtherApps: true) }
                .onDisappear { NSApp.setActivationPolicy(.accessory) }
        }
        .defaultSize(width: 1200, height: 760)
    }
}

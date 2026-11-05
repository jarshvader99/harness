import SwiftUI
import AppKit

@main
struct TurboHarnessApp: App {
    @StateObject private var runner = HarnessRunner()

    init() {
        if CommandLine.arguments.contains("--self-test") {
            SelfTest.run()
        }
        NSApplication.shared.setActivationPolicy(.regular)
    }

    var body: some Scene {
        WindowGroup("TurboHarness") {
            ContentView()
                .environmentObject(runner)
                .frame(minWidth: 1080, minHeight: 620)
                .onAppear {
                    NSApplication.shared.activate(ignoringOtherApps: true)
                    runner.refreshAll()
                }
        }
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("Harness") {
                Button("Run Mission") { runner.start() }
                    .keyboardShortcut("r", modifiers: [.command])
                    .disabled(runner.isRunning || runner.isChatting)
                Button("Stop Mission") { runner.stop() }
                    .keyboardShortcut(".", modifiers: [.command])
                    .disabled(!runner.isRunning)
                Divider()
                Button("New Chat") { runner.newChat() }
                Button("Close Chat") { runner.stopChat() }
                    .disabled(!runner.isChatting)
                Divider()
                Button("Show Mission") { runner.mode = .mission }
                    .keyboardShortcut("1", modifiers: [.command])
                Button("Show Chat") { runner.mode = .chat }
                    .keyboardShortcut("2", modifiers: [.command])
                Divider()
                Button("Refresh Files and History") { runner.refreshAll() }
                    .keyboardShortcut("r", modifiers: [.command, .shift])
            }
        }
    }
}

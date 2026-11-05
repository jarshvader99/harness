import Foundation

/// One line of the harness event stream (NDJSON from main.py via launch.py).
struct HarnessEvent: Identifiable {
    let id = UUID()
    let kind: String
    let text: String
    let time: Date
}

/// A past run, summarised from the footer of its JSONL trace.
struct RunRecord: Identifiable {
    let id: URL
    let mission: String
    let completed: Bool
    let iterations: Int
    let durationMs: Int
    let steps: Int
    let date: Date

    var durationLabel: String {
        durationMs < 1000 ? "\(durationMs) ms" : String(format: "%.1f s", Double(durationMs) / 1000)
    }
}

/// A file inside the sandbox directory.
struct SandboxFile: Identifiable, Hashable {
    let id: String
    let url: URL
    let size: Int
    let modified: Date

    var sizeLabel: String {
        size < 1024 ? "\(size) B" : String(format: "%.1f KB", Double(size) / 1024)
    }
}

enum AppMode: String, CaseIterable, Identifiable {
    case mission
    case chat

    var id: String { rawValue }

    var title: String {
        switch self {
        case .mission: return "Mission"
        case .chat: return "Chat"
        }
    }
}

enum HistoryTab: String, CaseIterable, Identifiable {
    case runs
    case chats

    var id: String { rawValue }
}

/// One turn in the chat pane.
struct ChatMessage: Identifiable {
    enum Role: String {
        case user
        case assistant
        case tool
        case error
        case note
    }

    let id = UUID()
    let role: Role
    let text: String
    let date: Date

    var label: String {
        switch role {
        case .user: return "You"
        case .assistant: return "Model"
        case .tool: return "Tool"
        case .error: return "Error"
        case .note: return "Harness"
        }
    }
}

/// A mission saved under tasks/, loaded back into the form.
struct SavedTask: Identifiable {
    let id: URL
    let name: String
    let goal: String
    let expectFile: String
    let expectContains: String
    let maxIterations: Int
}

/// A tool call the harness is holding until the user answers.
struct ApprovalRequest: Identifiable {
    let id: String
    let tool: String
    let command: String
    /// Which process asked, so the answer goes back to the right one even if the
    /// user switches between Mission and Chat while the question is on screen.
    let fromChat: Bool
}

/// A transcript under chats/, readable without a live model.
struct SavedChat: Identifiable {
    let id: URL
    let title: String
    let messages: Int
    let date: Date
}

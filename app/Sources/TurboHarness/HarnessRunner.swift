import Foundation
import Combine
import AppKit

@MainActor
final class HarnessRunner: ObservableObject {
    static let directoryKey = "harnessDirectory"
    static let projectKey = "projectDirectory"
    /// A whole repository is not worth walking to the last file on every refresh.
    static let fileLimit = 2000

    // MARK: - Mission state

    @Published var events: [HarnessEvent] = []
    @Published var runs: [RunRecord] = []
    @Published var files: [SandboxFile] = []
    @Published var isRunning = false
    @Published var status = "Idle"
    @Published var goal = "Create a file named test.txt"
    @Published var expectFile = "test.txt"
    @Published var expectContains = ""
    @Published var maxIterations = 8
    @Published var contextLimit = 16_384
    @Published var completed: Bool?
    @Published var modelLabel = "auto-detect"
    @Published var sandboxPath = ""
    @Published var filesTruncated = false
    @Published var selectedFileID: String?
    @Published var fileText = ""

    // MARK: - Navigation

    @Published var mode: AppMode = .mission
    @Published var historyTab: HistoryTab = .runs

    // MARK: - Saved tasks

    @Published var tasks: [SavedTask] = []
    @Published var taskName = ""
    @Published var showingSaveTask = false

    // MARK: - Chat

    @Published var chatMessages: [ChatMessage] = []
    @Published var chatBusy = false
    @Published var chatModel = "not started"
    @Published var chatTranscript = ""
    @Published var isChatting = false
    @Published var viewingSavedChat: String?
    @Published var savedChats: [SavedChat] = []

    // MARK: - Approvals

    /// Set while the harness is blocked on a yes/no from the user.
    @Published var pendingApproval: ApprovalRequest?
    /// Text of the answer being generated, so the pane fills in as it streams.
    @Published var streamingText = ""

    @Published var directory: URL {
        didSet {
            UserDefaults.standard.set(directory.path, forKey: Self.directoryKey)
            events.removeAll()
            completed = nil
            status = "Idle"
            refreshAll()
        }
    }

    private var process: Process?
    private var stdoutBuffer = Data()
    private var missionPipe: Pipe?
    private var chatProcess: Process?
    private var chatInput: FileHandle?
    private var chatBuffer = Data()

    /// Where the agent may read, write and run commands: the chosen project, or
    /// the harness's own sandbox/ when no project has been picked.
    @Published var projectPath: String {
        didSet {
            UserDefaults.standard.set(projectPath, forKey: Self.projectKey)
            refreshFiles()
        }
    }

    private var launcherURL: URL { directory.appendingPathComponent("launch.py") }
    private var sandboxURL: URL {
        let custom = projectPath.trimmingCharacters(in: .whitespacesAndNewlines)
        return custom.isEmpty ? directory.appendingPathComponent("sandbox")
                              : URL(fileURLWithPath: custom)
    }
    private var runsURL: URL { directory.appendingPathComponent("runs") }
    private var chatsURL: URL { directory.appendingPathComponent("chats") }
    private var tasksURL: URL { directory.appendingPathComponent("tasks") }

    init() {
        let defaults = UserDefaults.standard
        projectPath = defaults.string(forKey: Self.projectKey) ?? ""
        if let saved = defaults.string(forKey: Self.directoryKey),
           FileManager.default.fileExists(atPath: saved) {
            directory = URL(fileURLWithPath: saved)
        } else if let env = ProcessInfo.processInfo.environment["TURBO_HARNESS_DIR"], !env.isEmpty {
            directory = URL(fileURLWithPath: env)
        } else {
            directory = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Documents/harness")
        }
        refreshAll()
    }

    // MARK: - Missions

    func start() {
        guard !isRunning else { return }
        guard !isChatting else {
            status = "A chat is open — close it before running a mission."
            return
        }
        guard FileManager.default.fileExists(atPath: launcherURL.path) else {
            status = "launch.py not found in \(directory.path)"
            appendStderr("launch.py not found in \(directory.path)")
            return
        }

        events.removeAll()
        stdoutBuffer.removeAll()
        completed = nil
        fileText = ""
        selectedFileID = nil
        pendingApproval = nil
        streamingText = ""
        isRunning = true
        modelLabel = "connecting…"
        status = "Starting TurboFieldfare and the mission…"

        var arguments = ["-u", launcherURL.path, "--events",
                         "--goal", goal,
                         "--expect-file", expectFile,
                         "--sandbox", sandboxURL.path,
                         "--context", String(contextLimit),
                         "--max-iterations", String(maxIterations)]
        let contains = expectContains.trimmingCharacters(in: .whitespacesAndNewlines)
        if !contains.isEmpty {
            arguments += ["--expect-contains", contains]
        }

        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        task.arguments = ["python3"] + arguments
        task.currentDirectoryURL = directory

        let out = Pipe()
        let err = Pipe()
        let input = Pipe()
        task.standardOutput = out
        task.standardError = err
        // stdin carries approval answers, so the mission can stop and ask.
        task.standardInput = input
        missionPipe = input

        out.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            Task { @MainActor in self?.consume(data) }
        }
        err.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            let text = String(decoding: data, as: UTF8.self)
            Task { @MainActor in self?.appendStderr(text) }
        }
        task.terminationHandler = { [weak self] finished in
            let code = finished.terminationStatus
            Task { @MainActor in self?.finish(code: code) }
        }

        do {
            try task.run()
            process = task
        } catch {
            isRunning = false
            status = "Could not launch: \(error.localizedDescription)"
            appendStderr(status)
        }
    }

    func stop() {
        guard let process, process.isRunning else { return }
        status = "Stopping…"
        process.terminate()
    }

    private func finish(code: Int32) {
        isRunning = false
        process = nil
        missionPipe = nil
        if pendingApproval != nil {
            pendingApproval = nil
        }
        if completed == true {
            status = "Completed"
        } else if code == 0 {
            status = "Finished without validating"
        } else {
            status = "Failed (exit \(code))"
        }
        refreshAll()
    }

    // MARK: - Mission event stream

    private func consume(_ data: Data) {
        stdoutBuffer.append(data)
        while let newline = stdoutBuffer.firstIndex(of: 0x0A) {
            let lineData = stdoutBuffer[stdoutBuffer.startIndex..<newline]
            stdoutBuffer.removeSubrange(stdoutBuffer.startIndex...newline)
            if !lineData.isEmpty {
                ingest(line: Data(lineData))
            }
        }
    }

    private func ingest(line: Data) {
        guard let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else {
            events.append(HarnessEvent(kind: "raw", text: String(decoding: line, as: UTF8.self), time: Date()))
            return
        }
        let kind = object["type"] as? String ?? "log"
        let text = object["text"] as? String ?? ""
        switch kind {
        case "model": modelLabel = text
        case "mission": status = text
        case "finished": completed = object["completed"] as? Bool
        case "approval_request":
            pendingApproval = ApprovalRequest(
                id: object["id"] as? String ?? "apr",
                tool: object["tool"] as? String ?? "run_shell",
                command: object["command"] as? String ?? text,
                fromChat: false
            )
            status = "Waiting for your approval"
        case "generating":
            status = text
        default: break
        }
        events.append(HarnessEvent(kind: kind, text: text, time: Date()))
    }

    // MARK: - Approvals

    /// Answer the pending request; "always" lets the rest of the session run unprompted.
    func respond(approval decision: String) {
        guard let pending = pendingApproval else { return }
        let payload: [String: Any] = ["type": "approval", "id": pending.id, "decision": decision]
        if pending.fromChat {
            writeChat(payload)
            chatMessages.append(ChatMessage(
                role: .note,
                text: decision == "deny" || decision == "timeout"
                    ? "You declined: \(pending.command)"
                    : "You allowed\(decision == "always" ? " (and the rest of this session)" : ""): \(pending.command)",
                date: Date()))
        } else {
            writeMission(payload)
            events.append(HarnessEvent(
                kind: "approval",
                text: (decision == "deny" ? "Declined: " : "Allowed: ") + pending.command,
                time: Date()))
        }
        pendingApproval = nil
        status = decision == "deny" ? "Command declined" : "Command approved"
    }

    private func writeMission(_ payload: [String: Any]) {
        send(payload, to: missionPipe?.fileHandleForWriting)
    }

    private func send(_ payload: [String: Any], to handle: FileHandle?) {
        guard let handle,
              let data = try? JSONSerialization.data(withJSONObject: payload),
              var line = String(data: data, encoding: .utf8) else { return }
        line += "\n"
        try? handle.write(contentsOf: Data(line.utf8))
    }

    private func appendStderr(_ text: String) {
        for line in text.split(separator: "\n") where !line.isEmpty {
            events.append(HarnessEvent(kind: "stderr", text: String(line), time: Date()))
        }
    }

    // MARK: - Chat

    func sendChat(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        guard !isRunning else {
            status = "A mission is running — stop it before chatting."
            return
        }
        if viewingSavedChat != nil {
            newChat()
        }
        if chatProcess == nil {
            startChatSession()
        }
        chatMessages.append(ChatMessage(role: .user, text: trimmed, date: Date()))
        chatBusy = true
        status = "Chatting…"
        writeChat(["type": "user", "text": trimmed])
    }

    func startChatSession() {
        guard chatProcess == nil else { return }
        guard FileManager.default.fileExists(atPath: launcherURL.path) else {
            chatMessages.append(ChatMessage(role: .error, text: "launch.py not found in \(directory.path)",
                                            date: Date()))
            return
        }

        chatBuffer.removeAll()
        chatBusy = true
        chatModel = "starting…"
        chatTranscript = ""
        viewingSavedChat = nil
        streamingText = ""
        pendingApproval = nil

        let arguments = ["-u", launcherURL.path, "--events", "--serve-chat",
                         "--sandbox", sandboxURL.path,
                         "--context", String(contextLimit)]
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        task.arguments = ["python3"] + arguments
        task.currentDirectoryURL = directory

        let out = Pipe()
        let err = Pipe()
        let input = Pipe()
        task.standardOutput = out
        task.standardError = err
        task.standardInput = input

        out.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            Task { @MainActor in self?.consumeChat(data) }
        }
        err.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            let text = String(decoding: data, as: UTF8.self)
            Task { @MainActor in self?.appendChatStderr(text) }
        }
        task.terminationHandler = { [weak self] finished in
            let code = finished.terminationStatus
            Task { @MainActor in self?.chatFinished(code: code) }
        }

        do {
            try task.run()
            chatProcess = task
            chatInput = input.fileHandleForWriting
            isChatting = true
            status = "Starting the local model…"
        } catch {
            chatBusy = false
            chatModel = "not started"
            chatMessages.append(ChatMessage(role: .error,
                                            text: "Could not start the chat: \(error.localizedDescription)",
                                            date: Date()))
        }
    }

    /// Start over without a live conversation: clears the pane and resets the model's memory.
    func newChat() {
        // A pending question would otherwise block the reset until it times out.
        if let pending = pendingApproval, pending.fromChat {
            respond(approval: "deny")
        }
        if chatProcess != nil {
            writeChat(["type": "reset"])
        }
        chatMessages.removeAll()
        viewingSavedChat = nil
        chatBusy = false
        streamingText = ""
        pendingApproval = nil
        status = "Chat cleared"
    }

    func stopChat() {
        guard let process = chatProcess else { return }
        status = "Stopping the chat…"
        writeChat(["type": "quit"])
        // The launcher shuts its server down on its way out; only force it if that hangs.
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 15_000_000_000)
            if process.isRunning {
                process.terminate()
                status = "Chat process did not exit; terminated."
            }
        }
    }

    private func writeChat(_ payload: [String: Any]) {
        send(payload, to: chatInput)
    }

    private func consumeChat(_ data: Data) {
        chatBuffer.append(data)
        while let newline = chatBuffer.firstIndex(of: 0x0A) {
            let lineData = chatBuffer[chatBuffer.startIndex..<newline]
            chatBuffer.removeSubrange(chatBuffer.startIndex...newline)
            if !lineData.isEmpty {
                ingestChat(line: Data(lineData))
            }
        }
    }

    private func ingestChat(line: Data) {
        guard let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else {
            let text = String(decoding: line, as: UTF8.self)
            if !text.isEmpty {
                chatMessages.append(ChatMessage(role: .note, text: text, date: Date()))
            }
            return
        }
        let kind = object["type"] as? String ?? "log"
        let text = object["text"] as? String ?? ""

        switch kind {
        case "chat_ready":
            chatModel = object["model"] as? String ?? "local model"
            chatTranscript = object["transcript"] as? String ?? ""
            chatBusy = false
            streamingText = ""
            status = "Chat ready"
            refreshChats()
        case "chat_delta":
            streamingText += text
        case "approval_request":
            pendingApproval = ApprovalRequest(
                id: object["id"] as? String ?? "apr",
                tool: object["tool"] as? String ?? "run_shell",
                command: object["command"] as? String ?? text,
                fromChat: true
            )
            status = "Waiting for your approval"
        case "chat":
            let role = object["role"] as? String ?? "assistant"
            if role == "assistant" {
                // The logged text replaces the preview that was streaming in.
                streamingText = ""
                if !text.isEmpty {
                    chatMessages.append(ChatMessage(role: .assistant, text: text, date: Date()))
                }
                chatBusy = false
                refreshChats()
            }
        case "tool":
            chatMessages.append(ChatMessage(role: .tool, text: text, date: Date()))
            chatBusy = true
        case "result":
            chatMessages.append(ChatMessage(role: .tool, text: shortened(text, 420), date: Date()))
            refreshFiles()
        case "chat_status":
            let busy = text != "idle" && text != "error"
            chatBusy = busy
            if busy { status = "Thinking…" } else if status == "Thinking…" { status = "Chat ready" }
            if let error = object["error"] as? String, !error.isEmpty {
                chatMessages.append(ChatMessage(role: .error, text: error, date: Date()))
            }
        case "chat_reset":
            chatMessages.removeAll()
            chatBusy = false
        case "model":
            chatModel = text
        case "error":
            chatMessages.append(ChatMessage(role: .error, text: text, date: Date()))
            chatBusy = false
        case "warning", "log":
            if !text.isEmpty {
                chatMessages.append(ChatMessage(role: .note, text: text, date: Date()))
            }
        default:
            break
        }
    }

    private func appendChatStderr(_ text: String) {
        for line in text.split(separator: "\n") where !line.isEmpty {
            chatMessages.append(ChatMessage(role: .note, text: String(line), date: Date()))
        }
    }

    private func chatFinished(code: Int32) {
        chatInput = nil
        chatProcess = nil
        isChatting = false
        chatBusy = false
        streamingText = ""
        pendingApproval = nil
        if code != 0 {
            chatMessages.append(ChatMessage(role: .error,
                                            text: "Chat process exited with status \(code).",
                                            date: Date()))
        }
        if chatModel == "starting…" || chatModel.isEmpty {
            chatModel = "not started"
        }
        if status != "Chat cleared" {
            status = code == 0 ? "Chat closed" : "Chat failed (exit \(code))"
        }
        refreshChats()
        refreshFiles()
    }

    // MARK: - Saved tasks

    func refreshTasks() {
        var found: [SavedTask] = []
        let urls = (try? FileManager.default.contentsOfDirectory(
            at: tasksURL, includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles])) ?? []

        for url in urls where url.pathExtension == "json" {
            guard let data = try? Data(contentsOf: url),
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let taskGoal = object["goal"] as? String, !taskGoal.isEmpty else { continue }
            found.append(SavedTask(
                id: url,
                name: (object["name"] as? String) ?? url.deletingPathExtension().lastPathComponent,
                goal: taskGoal,
                expectFile: (object["expect_file"] as? String) ?? "test.txt",
                expectContains: (object["expect_contains"] as? String) ?? "",
                maxIterations: (object["max_iterations"] as? Int) ?? 8
            ))
        }
        tasks = found.sorted { $0.name.lowercased() < $1.name.lowercased() }
    }

    func loadTask(_ task: SavedTask) {
        goal = task.goal
        expectFile = task.expectFile
        expectContains = task.expectContains
        maxIterations = task.maxIterations
        mode = .mission
        status = "Loaded “\(task.name)”"
    }

    func saveCurrentTask(named rawName: String) {
        let name = rawName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else {
            status = "Give the task a name first."
            return
        }
        guard !goal.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !expectFile.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            status = "A task needs a goal and a success file."
            return
        }

        var payload: [String: Any] = [
            "name": name,
            "goal": goal,
            "expect_file": expectFile,
            "max_iterations": maxIterations,
        ]
        let contains = expectContains.trimmingCharacters(in: .whitespacesAndNewlines)
        if !contains.isEmpty { payload["expect_contains"] = contains }

        guard let data = try? JSONSerialization.data(
            withJSONObject: payload, options: [.prettyPrinted, .sortedKeys]) else { return }

        do {
            try FileManager.default.createDirectory(at: tasksURL, withIntermediateDirectories: true)
            try data.write(to: tasksURL.appendingPathComponent(slug(name) + ".json"))
            refreshTasks()
            status = "Saved “\(name)”"
        } catch {
            status = "Could not save the task: \(error.localizedDescription)"
        }
    }

    func deleteTask(_ task: SavedTask) {
        do {
            try FileManager.default.removeItem(at: task.id)
            refreshTasks()
            status = "Deleted “\(task.name)”"
        } catch {
            status = "Could not delete the task: \(error.localizedDescription)"
        }
    }

    private func slug(_ name: String) -> String {
        let mapped = name.lowercased().map { $0.isLetter || $0.isNumber ? $0 : "-" }
        let collapsed = String(mapped).split(separator: "-").joined(separator: "-")
        return collapsed.isEmpty ? "task" : collapsed
    }

    // MARK: - Files, runs and chats

    func refreshAll() {
        sandboxPath = sandboxURL.path
        refreshFiles()
        refreshRuns()
        refreshChats()
        refreshTasks()
    }

    /// Generated or vendored folders: their contents are never worth listing.
    nonisolated private static let heavyDirectories: Set<String> = [
        "node_modules", "bower_components", ".build", ".swiftpm", "DerivedData",
        "build", "dist", "target", "Pods", "Carthage", "vendor",
        "__pycache__", ".venv", "venv", ".tox", ".pytest_cache", ".mypy_cache",
        ".ruff_cache", ".next", ".nuxt", ".svelte-kit", ".parcel-cache",
        ".turbo", ".gradle", ".cargo",
    ]

    /// True for paths the pane leaves out even when git would list them.
    nonisolated private static func skips(relativePath: String) -> Bool {
        let parts = relativePath.split(separator: "/", omittingEmptySubsequences: true)
        guard let name = parts.last.map(String.init), name != ".DS_Store" else { return true }
        return parts.contains { $0 == ".git" || heavyDirectories.contains(String($0)) }
    }

    /// The physical path of a folder.
    ///
    /// `resolvingSymlinksInPath()` leaves `/var` alone while the enumerator
    /// returns `/private/var`, so the two never prefix-match; `realpath(3)`
    /// resolves both to the same string.
    nonisolated private static func canonical(_ url: URL) -> URL {
        guard let buffer = realpath(url.path, nil) else { return url.standardizedFileURL }
        defer { free(buffer) }
        return URL(fileURLWithPath: String(cString: buffer))
    }

    /// What git would keep: tracked files plus untracked ones that are not ignored.
    /// nil when the folder is not inside a repository, or git is unavailable.
    nonisolated private func gitListedFiles(in root: URL) -> [String]? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["git", "-C", root.path, "ls-files", "-z",
                             "--cached", "--others", "--exclude-standard"]
        let out = Pipe()
        process.standardOutput = out
        process.standardError = Pipe()
        do { try process.run() } catch { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { return nil }
        return String(decoding: data, as: UTF8.self)
            .split(separator: "\0", omittingEmptySubsequences: true)
            .map(String.init)
    }

    func refreshFiles() {
        // Work from the physical root, or every relative path comes back with a
        // stray "/private" on the front.
        let root = Self.canonical(sandboxURL)
        let keys: Set<URLResourceKey> = [.isRegularFileKey, .isDirectoryKey,
                                         .fileSizeKey, .contentModificationDateKey]
        var found: [SandboxFile] = []
        var truncated = false

        func add(_ url: URL, _ relative: String) {
            guard let values = try? url.resourceValues(forKeys: keys),
                  values.isRegularFile == true else { return }
            found.append(SandboxFile(id: relative, url: url,
                                     size: values.fileSize ?? 0,
                                     modified: values.contentModificationDate ?? .distantPast))
        }

        if let listed = gitListedFiles(in: root) {
            // In a repository, git answers the ignore question better than any
            // matcher could: nested .gitignore files, negations and info/exclude.
            for relative in listed {
                if found.count >= Self.fileLimit { truncated = true; break }
                guard !Self.skips(relativePath: relative) else { continue }
                add(root.appendingPathComponent(relative), relative)
            }
        } else if let enumerator = FileManager.default.enumerator(
            at: root, includingPropertiesForKeys: Array(keys), options: []
        ) {
            // No repository: walk, pruning the heavy folders outright.
            let prefix = root.path.hasSuffix("/") ? root.path : root.path + "/"
            for case let url as URL in enumerator {
                if found.count >= Self.fileLimit { truncated = true; break }
                guard let values = try? url.resourceValues(forKeys: keys) else { continue }
                if values.isDirectory == true {
                    let name = url.lastPathComponent
                    if name == ".git" || Self.heavyDirectories.contains(name) {
                        enumerator.skipDescendants()
                    }
                    continue
                }
                guard values.isRegularFile == true,
                      url.lastPathComponent != ".DS_Store" else { continue }
                let relative = url.path.hasPrefix(prefix)
                    ? String(url.path.dropFirst(prefix.count))
                    : url.lastPathComponent
                guard !Self.skips(relativePath: relative) else { continue }
                add(url, relative)
            }
        }

        filesTruncated = truncated
        files = found.sorted { $0.id < $1.id }
        if let id = selectedFileID, !files.contains(where: { $0.id == id }) {
            selectedFileID = nil
            fileText = ""
        }
    }

    func refreshRuns() {
        var records: [RunRecord] = []
        let urls = (try? FileManager.default.contentsOfDirectory(
            at: runsURL, includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles])) ?? []

        for url in urls where url.pathExtension == "jsonl" {
            let lines = (try? String(contentsOf: url, encoding: .utf8))?
                .split(separator: "\n") ?? []
            var mission = "—"
            var completed = false
            var iterations = 0
            var duration = 0
            var steps = 0
            for line in lines {
                guard let data = line.data(using: .utf8),
                      let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                else { continue }
                switch object["type"] as? String {
                case "run_start":
                    mission = object["mission"] as? String ?? mission
                case "run_end":
                    completed = object["completed"] as? Bool ?? false
                    iterations = object["iterations"] as? Int ?? 0
                    duration = object["duration_ms"] as? Int ?? 0
                    steps = object["steps"] as? Int ?? 0
                default:
                    break
                }
            }
            let date = (try? url.resourceValues(forKeys: [.contentModificationDateKey])
                .contentModificationDate) ?? Date()
            records.append(RunRecord(id: url, mission: mission, completed: completed,
                                     iterations: iterations, durationMs: duration,
                                     steps: steps, date: date))
        }
        runs = records.sorted { $0.date > $1.date }
    }

    func refreshChats() {
        var found: [SavedChat] = []
        let urls = (try? FileManager.default.contentsOfDirectory(
            at: chatsURL, includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles])) ?? []

        for url in urls where url.pathExtension == "jsonl" {
            let lines = (try? String(contentsOf: url, encoding: .utf8))?
                .split(separator: "\n") ?? []
            var title = ""
            var count = 0
            for line in lines {
                guard let data = line.data(using: .utf8),
                      let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      object["type"] as? String == "message" else { continue }
                count += 1
                if title.isEmpty, object["role"] as? String == "user" {
                    title = String((object["text"] as? String ?? "").prefix(90))
                }
            }
            guard count > 0 else { continue }
            let date = (try? url.resourceValues(forKeys: [.contentModificationDateKey])
                .contentModificationDate) ?? Date()
            found.append(SavedChat(id: url, title: title.isEmpty ? "Chat" : title,
                                   messages: count, date: date))
        }
        savedChats = found.sorted { $0.date > $1.date }
    }

    func loadChat(_ chat: SavedChat) {
        let lines = (try? String(contentsOf: chat.id, encoding: .utf8))?
            .split(separator: "\n") ?? []
        var messages: [ChatMessage] = []
        for line in lines {
            guard let data = line.data(using: .utf8),
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  object["type"] as? String == "message" else { continue }
            let role = ChatMessage.Role(rawValue: object["role"] as? String ?? "assistant") ?? .assistant
            let date = (object["time"] as? String).flatMap(Self.parseTimestamp) ?? Date()
            messages.append(ChatMessage(role: role, text: object["text"] as? String ?? "", date: date))
        }
        guard !messages.isEmpty else { return }
        mode = .chat
        chatMessages = messages
        viewingSavedChat = chat.title
        chatTranscript = chat.id.path
        streamingText = ""
        status = "Reading a saved chat"
    }

    private static func parseTimestamp(_ text: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: text) ?? ISO8601DateFormatter().date(from: text)
    }

    func select(_ file: SandboxFile) {
        selectedFileID = file.id
        guard file.size <= 512_000 else {
            fileText = "(\(file.size) bytes — too large to display)"
            return
        }
        fileText = (try? String(contentsOf: file.url, encoding: .utf8)) ?? "(not UTF-8 text)"
    }

    func chooseDirectory() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = directory
        panel.prompt = "Use this folder"
        panel.message = "Choose the folder holding main.py, launch.py and runs/"
        if panel.runModal() == .OK, let chosen = panel.url {
            directory = chosen
        }
    }

    /// Point the agent at a codebase: reads, writes and shell commands all run there.
    func chooseProject() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = sandboxURL
        panel.prompt = "Work in this folder"
        panel.message = "Choose the project the agent may read, write and run commands in"
        if panel.runModal() == .OK, let chosen = panel.url {
            projectPath = chosen.path
            sandboxPath = chosen.path
            status = "Project: \(chosen.lastPathComponent)"
        }
    }

    /// Back to the harness's own sandbox/ folder.
    func useDefaultProject() {
        projectPath = ""
        sandboxPath = sandboxURL.path
        status = "Project: sandbox"
    }

    var isDefaultProject: Bool {
        projectPath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var projectName: String {
        isDefaultProject ? "sandbox" : sandboxURL.lastPathComponent
    }

    private func shortened(_ text: String, _ limit: Int) -> String {
        text.count <= limit ? text : String(text.prefix(limit)) + "…"
    }
}

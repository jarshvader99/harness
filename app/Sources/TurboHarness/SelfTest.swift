import Foundation

/// `TurboHarness --self-test` runs one mission and one chat turn through the
/// same runner the UI uses, then prints what the panes would show. It is the
/// only way to verify the process bridge without clicking.
enum SelfTest {
    static func run() -> Never {
        MainActor.assumeIsolated {
            let runner = HarnessRunner()
            print("directory: \(runner.directory.path)")
            print("tasks: \(runner.tasks.map { $0.name })")
            print("saved chats: \(runner.savedChats.count)")

            // --- saved tasks -------------------------------------------------
            print("\n== saved tasks ==")
            runner.goal = "Create a file named test.txt"
            runner.expectFile = "test.txt"
            runner.expectContains = ""
            runner.maxIterations = 3
            runner.saveCurrentTask(named: "Self test task")
            runner.refreshTasks()
            guard let saved = runner.tasks.first(where: { $0.name == "Self test task" }) else {
                print("FAILED: the saved task was not listed")
                exit(1)
            }
            print("listed: \(saved.name) goal=\(saved.goal) turns=\(saved.maxIterations)")

            runner.goal = "wiped"
            runner.maxIterations = 1
            runner.loadTask(saved)
            print("loaded back: goal=\(runner.goal) turns=\(runner.maxIterations)")
            guard runner.goal == saved.goal, runner.maxIterations == saved.maxIterations else {
                print("FAILED: loading a task did not restore the form")
                exit(1)
            }
            runner.deleteTask(saved)
            runner.refreshTasks()
            print("after delete: \(runner.tasks.map { $0.name })")

            // --- file pane: .gitignore and heavy folders ---------------------
            print("\n== file pane ==")
            let repo = URL(fileURLWithPath: NSTemporaryDirectory())
                .appendingPathComponent("turbo-selftest-repo-\(UUID().uuidString.prefix(8))")
                .standardizedFileURL
            try? FileManager.default.createDirectory(at: repo, withIntermediateDirectories: true)
            for folder in ["src", "node_modules/pkg", "build"] {
                try? FileManager.default.createDirectory(
                    at: repo.appendingPathComponent(folder), withIntermediateDirectories: true)
            }
            let seeded: [String: String] = [
                "kept.txt": "kept\n",
                "ignored.txt": "ignored\n",
                "run.log": "log\n",
                "src/app.js": "app\n",
                "node_modules/pkg/index.js": "dependency\n",
                "build/out.js": "generated\n",
                ".gitignore": "ignored.txt\n*.log\n",
            ]
            for (path, body) in seeded {
                try? body.write(to: repo.appendingPathComponent(path),
                                atomically: true, encoding: .utf8)
            }
            let gitStatus = Self.shell("git init -q . && git add kept.txt src/app.js", in: repo)
            print("git init status: \(gitStatus)")

            runner.projectPath = repo.path
            runner.refreshFiles()
            let paneFiles = Set(runner.files.map { $0.id })
            print("pane lists: \(paneFiles.sorted())")
            for wanted in ["kept.txt", "src/app.js", ".gitignore"] where !paneFiles.contains(wanted) {
                print("FAILED: the pane should list \(wanted)")
                exit(1)
            }
            for unwanted in ["ignored.txt", "run.log",
                             "node_modules/pkg/index.js", "build/out.js"]
            where paneFiles.contains(unwanted) {
                print("FAILED: the pane still lists \(unwanted)")
                exit(1)
            }
            print(".gitignore honoured, heavy folders skipped: true")
            try? FileManager.default.removeItem(at: repo)

            // The same folder rule has to hold outside a repository, where there
            // is no git to ask: the heavy folders are pruned by name.
            let plain = URL(fileURLWithPath: NSTemporaryDirectory())
                .appendingPathComponent("turbo-selftest-plain-\(UUID().uuidString.prefix(8))")
                .standardizedFileURL
            try? FileManager.default.createDirectory(
                at: plain.appendingPathComponent("node_modules/x"), withIntermediateDirectories: true)
            try? FileManager.default.createDirectory(
                at: plain.appendingPathComponent("scripts"), withIntermediateDirectories: true)
            try? "a\n".write(to: plain.appendingPathComponent("a.txt"),
                              atomically: true, encoding: .utf8)
            try? "dep\n".write(to: plain.appendingPathComponent("node_modules/x/i.js"),
                                atomically: true, encoding: .utf8)
            try? "b\n".write(to: plain.appendingPathComponent("scripts/b.sh"),
                              atomically: true, encoding: .utf8)
            runner.projectPath = plain.path
            runner.refreshFiles()
            let plainListed = Set(runner.files.map { $0.id })
            print("no repo, pane lists: \(plainListed.sorted())")
            guard plainListed.contains("a.txt"), plainListed.contains("scripts/b.sh"),
                  !plainListed.contains("node_modules/x/i.js") else {
                print("FAILED: heavy folders are not pruned outside a repository")
                exit(1)
            }
            print("heavy folders pruned without git: true")
            try? FileManager.default.removeItem(at: plain)

            // --- mission, in a scratch project, including an approval --------
            print("\n== mission ==")
            // A throwaway project: an empty folder, so the success check cannot
            // pass before a tool runs, and outside the harness's own sandbox/ so
            // the project plumbing is what is being tested.
            let project = URL(fileURLWithPath: NSTemporaryDirectory())
                .appendingPathComponent("turbo-selftest-\(UUID().uuidString.prefix(8))").standardizedFileURL
            try? FileManager.default.createDirectory(at: project, withIntermediateDirectories: true)
            // Clear the same name from sandbox/, so the check below proves a new
            // write went to the project rather than finding an old file.
            try? FileManager.default.removeItem(
                at: runner.directory.appendingPathComponent("sandbox/approved.txt"))
            runner.projectPath = project.path
            runner.refreshFiles()
            print("project: \(project.path) (pane sees \(runner.files.count) files)")

            runner.goal = "Run the shell command: echo approved > approved.txt . Then stop."
            runner.expectFile = "approved.txt"
            runner.expectContains = "approved"
            runner.maxIterations = 3
            print("mission: \(runner.goal)")
            runner.start()

            var approvals = 0
            let missionDeadline = Date().addingTimeInterval(400)
            while runner.isRunning && Date() < missionDeadline {
                RunLoop.main.run(until: Date().addingTimeInterval(0.2))
                if let pending = runner.pendingApproval {
                    approvals += 1
                    print("approval requested: \(pending.tool) -> \(pending.command)")
                    runner.respond(approval: "allow")
                }
            }

            print("events:")
            for event in runner.events {
                print("  [\(event.kind)] \(event.text.prefix(110))")
            }
            print("completed: \(runner.completed.map { String($0) } ?? "nil")")
            print("status: \(runner.status)")
            print("model: \(runner.modelLabel)")
            print("files: \(runner.files.map { $0.id })")
            print("runs: \(runner.runs.count) \(runner.runs.first.map { "\($0.mission) completed=\($0.completed) steps=\($0.steps)" } ?? "")")
            print("file text: \(runner.files.first.map { runner.select($0); return runner.fileText } ?? "(none)")")

            if runner.completed != true {
                print("FAILED: the mission did not validate")
                exit(1)
            }
            guard approvals > 0 else {
                print("FAILED: the mission never asked for approval")
                exit(1)
            }
            print("approvals answered: \(approvals)")
            print("approval logged: \(runner.events.contains { $0.kind == "approval" })")
            print("approval event text: \(runner.events.first { $0.kind == "approval_request" }?.text ?? "(none)")")
            guard runner.files.contains(where: { $0.id == "approved.txt" }) else {
                print("FAILED: the approved command did not run")
                exit(1)
            }
            guard FileManager.default.fileExists(
                atPath: project.appendingPathComponent("approved.txt").path) else {
                print("FAILED: the file was not written in the project folder")
                exit(1)
            }
            print("wrote into the project, not sandbox/: \(runner.projectName)")
            guard !FileManager.default.fileExists(
                atPath: runner.directory.appendingPathComponent("sandbox/approved.txt").path) else {
                print("FAILED: the file also landed in the harness sandbox")
                exit(1)
            }
            guard runner.events.contains(where: {
                $0.kind == "log" && $0.text.contains(project.path)
            }) else {
                print("FAILED: the harness was not given the project path")
                exit(1)
            }
            print("harness reported the project path: true")

            // --- chat --------------------------------------------------------
            print("\n== chat ==")
            runner.mode = .chat
            runner.sendChat("Answer with exactly one word: READY. Do not call any function.")

            var streamed = 0
            let chatDeadline = Date().addingTimeInterval(420)
            while Date() < chatDeadline {
                RunLoop.main.run(until: Date().addingTimeInterval(0.2))
                streamed = max(streamed, runner.streamingText.count)
                let answered = runner.chatMessages.contains { $0.role == .assistant }
                let failed = runner.chatMessages.contains { $0.role == .error }
                if (answered || failed) && !runner.chatBusy { break }
            }
            print("streamed characters seen: \(streamed)")
            guard streamed > 0 else {
                print("FAILED: no text streamed into the pane")
                exit(1)
            }

            for message in runner.chatMessages {
                print("  [\(message.role.rawValue)] \(message.text.prefix(160))")
            }
            print("chat model: \(runner.chatModel)")
            print("chat transcript: \(runner.chatTranscript)")
            print("chatting: \(runner.isChatting)")

            guard runner.chatMessages.contains(where: { $0.role == .assistant }) else {
                print("FAILED: no assistant reply in the chat")
                exit(1)
            }
            guard !runner.chatTranscript.isEmpty,
                  FileManager.default.fileExists(atPath: runner.chatTranscript) else {
                print("FAILED: the chat transcript was not written")
                exit(1)
            }
            let listed = runner.savedChats.contains { $0.id.path == runner.chatTranscript }
            print("chat listed in history: \(listed)")

            runner.stopChat()
            let closeDeadline = Date().addingTimeInterval(40)
            while runner.isChatting && Date() < closeDeadline {
                RunLoop.main.run(until: Date().addingTimeInterval(0.2))
            }
            print("chat closed: \(!runner.isChatting) status=\(runner.status)")

            // Do not leave the app pointed at a temp folder that is about to go.
            runner.useDefaultProject()
            try? FileManager.default.removeItem(at: project)
            print("project reset to: \(runner.projectName) (\(runner.sandboxPath))")

            print("\nself-test passed")
        }
        exit(0)
    }

    private static func shell(_ command: String, in directory: URL) -> Int32 {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = ["-c", command]
        process.currentDirectoryURL = directory
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { return -1 }
        process.waitUntilExit()
        return process.terminationStatus
    }
}

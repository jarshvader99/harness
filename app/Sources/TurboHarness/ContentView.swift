import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var runner: HarnessRunner
    @State private var draft = ""

    var body: some View {
        VStack(spacing: 0) {
            modeBar
            Divider()
            HSplitView {
                sidebar
                    .frame(minWidth: 260, idealWidth: 290, maxWidth: 360)
                Group {
                    switch runner.mode {
                    case .mission: logPane
                    case .chat: chatPane
                    }
                }
                .frame(minWidth: 440)
                .overlay(alignment: .bottom) { approvalOverlay }
                filesPane
                    .frame(minWidth: 260, idealWidth: 320, maxWidth: 460)
            }
            Divider()
            statusBar
        }
    }

    // MARK: - Mode

    private var modeBar: some View {
        HStack(spacing: 12) {
            Picker("", selection: $runner.mode) {
                ForEach(AppMode.allCases) { mode in
                    Text(mode.title).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 180)

            Text(runner.mode == .mission
                 ? "One goal, a turn budget, and a success check."
                 : "Talk to the model; it can use the same tools a mission does.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)

            Spacer()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
    }

    // MARK: - Left: mission form, saved tasks, history

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            if runner.mode == .mission {
                missionForm
            } else {
                chatForm
            }

            Divider()
            historySection
        }
    }

    private var missionForm: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text("MISSION")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)

            TextField("What should the agent do?", text: $runner.goal, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(2...5)

            TextField("Success: file exists", text: $runner.expectFile)
                .textFieldStyle(.roundedBorder)
                .help("Relative to \(runner.projectName)/")

            TextField("…and contains (optional)", text: $runner.expectContains)
                .textFieldStyle(.roundedBorder)

            HStack {
                Stepper("Max \(runner.maxIterations) turns", value: $runner.maxIterations, in: 1...40)
                    .font(.callout)
                Spacer()
            }

            HStack(spacing: 8) {
                Text("Context")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Picker("", selection: $runner.contextLimit) {
                    Text("8k").tag(8_192)
                    Text("16k").tag(16_384)
                    Text("32k").tag(32_768)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .help("The server's context window; older turns are compacted before it fills.")
            }

            HStack(spacing: 8) {
                Button {
                    runner.start()
                } label: {
                    Label("Run", systemImage: "play.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(runner.isRunning || runner.isChatting)

                Button {
                    runner.stop()
                } label: {
                    Label("Stop", systemImage: "stop.fill")
                }
                .disabled(!runner.isRunning)
            }

            HStack(spacing: 8) {
                Menu {
                    if runner.tasks.isEmpty {
                        Text("No saved tasks")
                    }
                    ForEach(runner.tasks) { task in
                        Button(task.name) { runner.loadTask(task) }
                    }
                    if !runner.tasks.isEmpty {
                        Divider()
                        Menu("Delete") {
                            ForEach(runner.tasks) { task in
                                Button(task.name) { runner.deleteTask(task) }
                            }
                        }
                    }
                } label: {
                    Label("Saved tasks", systemImage: "tray.full")
                }
                .menuStyle(.borderlessButton)
                .fixedSize()

                Spacer()

                Button {
                    runner.taskName = defaultTaskName
                    runner.showingSaveTask = true
                } label: {
                    Label("Save…", systemImage: "square.and.arrow.down")
                }
                .buttonStyle(.borderless)
            }
            .font(.callout)
        }
        .padding(14)
        .alert("Save mission", isPresented: $runner.showingSaveTask) {
            TextField("Task name", text: $runner.taskName)
            Button("Save") { runner.saveCurrentTask(named: runner.taskName) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Stored in tasks/ next to launch.py, so the CLI can run it with --task.")
        }
    }

    private var defaultTaskName: String {
        let words = runner.goal.split(separator: " ").prefix(5).joined(separator: " ")
        return words.isEmpty ? "New task" : String(words)
    }

    private var chatForm: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("CHAT")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)

            HStack(spacing: 6) {
                Circle()
                    .fill(runner.isChatting ? Color.green : Color.secondary)
                    .frame(width: 7, height: 7)
                Text(runner.chatModel)
                    .font(.callout)
                    .lineLimit(1)
            }

            Text(runner.isChatting
                 ? "The model is loaded and answers below. Close the chat to give the Mac app its memory back."
                 : "The model starts on your first message and shuts down when the chat closes.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 8) {
                Button {
                    runner.newChat()
                } label: {
                    Label("New chat", systemImage: "square.and.pencil")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)

                Button {
                    runner.stopChat()
                } label: {
                    Label("Close", systemImage: "xmark.circle")
                }
                .disabled(!runner.isChatting)
            }

            if !runner.chatTranscript.isEmpty {
                Text(URL(fileURLWithPath: runner.chatTranscript).lastPathComponent)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .help(runner.chatTranscript)
            }
        }
        .padding(14)
    }

    private var historySection: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Picker("", selection: $runner.historyTab) {
                    Text("Runs").tag(HistoryTab.runs)
                    Text("Chats").tag(HistoryTab.chats)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 140)

                Spacer()

                Button {
                    runner.refreshAll()
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .help("Reload files, tasks and history")
            }
            .padding(.horizontal, 14)
            .padding(.top, 10)
            .padding(.bottom, 6)

            switch runner.historyTab {
            case .runs: runsList
            case .chats: chatsList
            }
        }
    }

    private var runsList: some View {
        Group {
            if runner.runs.isEmpty {
                emptyNote("No runs yet.")
            } else {
                List(runner.runs) { run in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 6) {
                            Image(systemName: run.completed ? "checkmark.circle.fill" : "xmark.circle.fill")
                                .foregroundStyle(run.completed ? .green : .red)
                            Text(run.mission)
                                .font(.callout)
                                .lineLimit(2)
                        }
                        Text("\(run.steps) step\(run.steps == 1 ? "" : "s") · \(run.iterations) turn\(run.iterations == 1 ? "" : "s") · \(run.durationLabel)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Text(run.date.formatted(date: .abbreviated, time: .shortened))
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                    .padding(.vertical, 2)
                }
                .listStyle(.sidebar)
            }
        }
    }

    private var chatsList: some View {
        Group {
            if runner.savedChats.isEmpty {
                emptyNote("No saved chats yet.")
            } else {
                List(runner.savedChats) { chat in
                    Button {
                        runner.loadChat(chat)
                    } label: {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(chat.title)
                                .font(.callout)
                                .lineLimit(2)
                            Text("\(chat.messages) message\(chat.messages == 1 ? "" : "s") · \(chat.date.formatted(date: .abbreviated, time: .shortened))")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .padding(.vertical, 2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
                .listStyle(.sidebar)
            }
        }
    }

    private func emptyNote(_ text: String) -> some View {
        VStack {
            Text(text)
                .font(.callout)
                .foregroundStyle(.secondary)
            Spacer()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
    }

    // MARK: - Approvals

    /// The harness is blocked until this is answered, in either mode.
    @ViewBuilder
    private var approvalOverlay: some View {
        if let request = runner.pendingApproval {
            VStack(alignment: .leading, spacing: 9) {
                HStack(spacing: 7) {
                    Image(systemName: "lock.shield.fill")
                        .foregroundStyle(.orange)
                    Text("Approve this command?")
                        .font(.callout.weight(.semibold))
                    Spacer()
                    Text(request.tool)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }

                Text(request.command)
                    .font(.system(size: 12, design: .monospaced))
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
                    .background(
                        RoundedRectangle(cornerRadius: 6)
                            .fill(Color(nsColor: .textBackgroundColor))
                    )

                Text("It runs in the sandbox folder, as you, and nothing else can start until you answer.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)

                HStack(spacing: 8) {
                    Button("Deny", role: .cancel) { runner.respond(approval: "deny") }
                        .keyboardShortcut(.cancelAction)
                    Spacer()
                    Button("Always allow") { runner.respond(approval: "always") }
                        .help("Allow every command for the rest of this mission or chat")
                    Button("Allow") { runner.respond(approval: "allow") }
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut(.defaultAction)
                }
            }
            .padding(12)
            .frame(maxWidth: 540)
            .background(
                RoundedRectangle(cornerRadius: 10)
                    .fill(Color(nsColor: .windowBackgroundColor))
                    .shadow(color: .black.opacity(0.28), radius: 14, y: 5)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 10)
                    .stroke(Color.orange.opacity(0.7), lineWidth: 1)
            )
            .padding(18)
            .transition(.move(edge: .bottom).combined(with: .opacity))
        }
    }

    // MARK: - Centre: live log

    private var logPane: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 3) {
                    if runner.events.isEmpty {
                        Text("Run a mission to see the agent think, call tools and write files.")
                            .foregroundStyle(.secondary)
                            .padding(16)
                    }
                    ForEach(runner.events) { event in
                        EventRow(event: event)
                            .id(event.id)
                    }
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .background(Color(nsColor: .textBackgroundColor))
            .onChange(of: runner.events.count) { _, _ in
                if let last = runner.events.last {
                    withAnimation(.easeOut(duration: 0.12)) {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }
        }
    }

    // MARK: - Centre: chat

    private var chatPane: some View {
        VStack(spacing: 0) {
            if let saved = runner.viewingSavedChat {
                HStack(spacing: 8) {
                    Label("Reading a saved chat: \(saved)", systemImage: "clock.arrow.circlepath")
                        .font(.caption)
                        .lineLimit(1)
                    Spacer()
                    Button("Start a new chat") { runner.newChat() }
                        .font(.caption)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .background(Color(nsColor: .controlBackgroundColor))
            }

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        if runner.chatMessages.isEmpty && runner.streamingText.isEmpty {
                            VStack(alignment: .leading, spacing: 6) {
                                Text("Talk to the local model.")
                                    .font(.headline)
                                Text("It can read and write files in the sandbox, run shell commands and fetch web pages, the same tools a mission uses. Commands wait for your approval, everything it does shows up here as tool lines, and the file pane stays in sync.")
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            .padding(18)
                        }
                        ForEach(runner.chatMessages) { message in
                            ChatBubble(message: message)
                                .id(message.id)
                        }
                        if !runner.streamingText.isEmpty {
                            ChatBubble(message: ChatMessage(role: .assistant,
                                                            text: runner.streamingText,
                                                            date: Date()))
                                .id("streaming")
                        }
                        if runner.chatBusy && runner.streamingText.isEmpty {
                            HStack(spacing: 7) {
                                ProgressView().controlSize(.small)
                                Text(runner.isChatting
                                     ? (runner.pendingApproval == nil
                                        ? "Thinking…"
                                        : "Waiting for your approval…")
                                     : "Starting the model…")
                                    .font(.callout)
                                    .foregroundStyle(.secondary)
                            }
                            .padding(.leading, 6)
                            .id("busy")
                        }
                    }
                    .padding(14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .background(Color(nsColor: .textBackgroundColor))
                .onChange(of: runner.chatMessages.count) { _, _ in
                    if let last = runner.chatMessages.last {
                        withAnimation(.easeOut(duration: 0.12)) {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }
                .onChange(of: runner.chatBusy) { _, busy in
                    guard busy else { return }
                    withAnimation(.easeOut(duration: 0.12)) {
                        proxy.scrollTo("busy", anchor: .bottom)
                    }
                }
                .onChange(of: runner.streamingText.count) { _, _ in
                    proxy.scrollTo(runner.streamingText.isEmpty ? "busy" : "streaming",
                                   anchor: .bottom)
                }
                .onChange(of: runner.pendingApproval?.id) { _, _ in
                    withAnimation(.easeOut(duration: 0.15)) {
                        proxy.scrollTo("busy", anchor: .bottom)
                    }
                }
            }

            Divider()

            HStack(alignment: .bottom, spacing: 8) {
                TextField("Message the model…", text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(1...6)
                    .padding(8)
                    .background(
                        RoundedRectangle(cornerRadius: 8)
                            .fill(Color(nsColor: .textBackgroundColor))
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 8)
                            .stroke(Color(nsColor: .separatorColor), lineWidth: 1)
                    )
                    .onSubmit { sendDraft() }

                Button {
                    sendDraft()
                } label: {
                    Label("Send", systemImage: "paperplane.fill")
                }
                .buttonStyle(.borderedProminent)
                .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                          || runner.isRunning)
            }
            .padding(10)
        }
    }

    private func sendDraft() {
        let text = draft
        draft = ""
        runner.sendChat(text)
    }

    // MARK: - Right: sandbox files

    private var filesPane: some View {
        VSplitView {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 7) {
                    Text("PROJECT")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    Button {
                        runner.chooseProject()
                    } label: {
                        Label(runner.projectName, systemImage: "folder")
                            .font(.callout)
                    }
                    .buttonStyle(.borderless)
                    .help("Working folder: \(runner.sandboxPath) — click to change it. "
                          + "Files git ignores, and build folders like node_modules, are left out.")

                    if !runner.isDefaultProject {
                        Button {
                            runner.useDefaultProject()
                        } label: {
                            Image(systemName: "arrow.uturn.backward")
                        }
                        .buttonStyle(.borderless)
                        .help("Back to the harness's own sandbox/ folder")
                    }

                    Spacer()

                    if runner.filesTruncated {
                        Text("first \(HarnessRunner.fileLimit)")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                            .help("This folder holds more files than the pane lists. "
                                  + "Ignored files and build folders are not counted.")
                    }
                    Text("\(runner.files.count) file\(runner.files.count == 1 ? "" : "s")")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                .padding(.horizontal, 12)
                .padding(.top, 10)
                .padding(.bottom, 4)

                if runner.files.isEmpty {
                    Text("Nothing here yet.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .padding(12)
                    Spacer()
                } else {
                    List(selection: $runner.selectedFileID) {
                        ForEach(runner.files) { file in
                            HStack(spacing: 6) {
                                Image(systemName: "doc.text")
                                    .foregroundStyle(.secondary)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(file.id).font(.callout)
                                    Text(file.sizeLabel).font(.caption2).foregroundStyle(.tertiary)
                                }
                            }
                            .tag(file.id)
                            .onTapGesture { runner.select(file) }
                        }
                    }
                    .listStyle(.inset)
                }
            }
            .frame(minHeight: 160)

            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text(runner.selectedFileID ?? "No file selected")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    Spacer()
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 6)

                Divider()

                ScrollView([.vertical, .horizontal]) {
                    Text(runner.fileText)
                        .font(.system(size: 12, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                }
            }
            .frame(minHeight: 160)
        }
    }

    // MARK: - Status bar

    private var statusBar: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(statusTint)
                .frame(width: 8, height: 8)
            Text(runner.status)
                .font(.callout)
                .lineLimit(1)

            Spacer()

            Text(modelSummary)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)

            Button {
                runner.chooseProject()
            } label: {
                Label(runner.projectName, systemImage: "tray.full")
                    .font(.caption)
            }
            .buttonStyle(.borderless)
            .help("Project folder: \(runner.sandboxPath)")

            Button {
                runner.chooseDirectory()
            } label: {
                Label(runner.directory.lastPathComponent, systemImage: "folder")
                    .font(.caption)
            }
            .buttonStyle(.borderless)
            .help("Harness folder: \(runner.directory.path)")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
    }

    private var statusTint: Color {
        if runner.pendingApproval != nil { return .orange }
        if runner.isRunning || runner.chatBusy { return .orange }
        if runner.completed == true { return .green }
        return .secondary
    }

    private var modelSummary: String {
        if runner.mode == .chat {
            guard !runner.chatTranscript.isEmpty else { return runner.chatModel }
            return "\(runner.chatModel) · \(URL(fileURLWithPath: runner.chatTranscript).lastPathComponent)"
        }
        return runner.modelLabel
    }
}

private struct EventRow: View {
    let event: HarnessEvent

    var body: some View {
        HStack(alignment: .top, spacing: 7) {
            Image(systemName: symbol)
                .foregroundStyle(tint)
                .frame(width: 14)
                .padding(.top, 2)
            Text(event.text)
                .font(.system(size: 12, design: .monospaced))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, 1)
    }

    private var symbol: String {
        switch event.kind {
        case "mission": return "flag.checkered"
        case "task": return "tray.full"
        case "iteration": return "arrow.triangle.2.circlepath"
        case "thought": return "brain"
        case "tool": return "wrench.and.screwdriver"
        case "action": return "bolt"
        case "result": return "tray.and.arrow.down"
        case "success": return "checkmark.seal.fill"
        case "compaction": return "arrow.down.right.and.arrow.up.left"
        case "generating": return "ellipsis.circle"
        case "approval": return "checkmark.shield"
        case "approval_request": return "lock.shield"
        case "warning": return "exclamationmark.triangle"
        case "error": return "xmark.octagon.fill"
        case "model": return "cpu"
        case "finished": return "flag.fill"
        case "stderr": return "terminal"
        default: return "info.circle"
        }
    }

    private var tint: Color {
        switch event.kind {
        case "success": return .green
        case "error": return .red
        case "warning": return .orange
        case "tool": return .purple
        case "action": return .blue
        case "result": return .teal
        case "thought": return .secondary
        case "compaction": return .indigo
        case "generating": return .secondary
        case "approval": return .green
        case "approval_request": return .orange
        case "stderr": return .gray
        case "mission": return .accentColor
        case "task": return .accentColor
        default: return .secondary
        }
    }
}

private struct ChatBubble: View {
    let message: ChatMessage

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            if message.role == .user { Spacer(minLength: 40) }

            VStack(alignment: .leading, spacing: 3) {
                if message.role != .note {
                    Text(message.label)
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
                Text(message.text)
                    .font(.system(size: message.role == .tool || message.role == .note ? 11.5 : 12.5,
                                  design: message.role == .tool || message.role == .note ? .monospaced : .default))
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(message.role == .note ? 0 : 9)
            .background(
                RoundedRectangle(cornerRadius: 9)
                    .fill(background)
            )

            if message.role != .user { Spacer(minLength: 40) }
        }
    }

    private var background: Color {
        switch message.role {
        case .user: return Color.accentColor.opacity(0.16)
        case .assistant: return Color(nsColor: .controlBackgroundColor)
        case .tool: return Color(nsColor: .quaternaryLabelColor).opacity(0.08)
        case .error: return Color.red.opacity(0.12)
        case .note: return .clear
        }
    }
}

import SwiftUI
import UniformTypeIdentifiers

/// The Kanban board: three columns (To Do / In Progress / Done) with the
/// recurrent tasks in a separate collapsible bottom pane.
///
/// Phase 4 makes it interactive. Cards are `.draggable`, columns are drop
/// targets, and `⌘←`/`⌘→` move the selected card — but every one of those
/// routes goes through `Store.perform(drop:on:)` and therefore through
/// `BoardDropRules`, so the drop table is enforced once rather than three
/// times.
struct BoardView: View {
    @Environment(Store.self) private var store
    /// Non-nil when the sidebar selected a single role, which scopes the board.
    let roleFilter: String?

    /// The card currently being dragged.
    ///
    /// Set by the drag source (`DraggableIfAllowed`) and used only for *hover
    /// styling* — whether the column under the pointer lights up or shows its
    /// refusal. The drop itself reads the decoded payload that `.dropDestination`
    /// hands back, which is what actually crossed the drag boundary; this is a
    /// fallback for that.
    @State private var dragging: TaskDragPayload?
    /// The keyboard-selected card.
    @State private var selection: String?
    @FocusState private var boardFocused: Bool

    private var visibleTasks: [TrackerTask] {
        guard let roleFilter else { return store.tasks }
        return store.tasks.filter { $0.roleId == roleFilter }
    }

    /// What the column shows, after the current scope.
    private func column(_ status: TaskStatus) -> [TrackerTask] {
        let scoped = store.boardTasks(status)
        guard let roleFilter else { return scoped }
        return scoped.filter { $0.roleId == roleFilter }
    }

    private var recurrent: [TrackerTask] {
        guard let roleFilter else { return store.recurrentTasks }
        return store.recurrentTasks.filter { $0.roleId == roleFilter }
    }

    var body: some View {
        @Bindable var store = store
        VSplitView {
            VStack(spacing: 0) {
                if let feedback = store.feedback {
                    FeedbackBar(feedback: feedback) { store.clearFeedback() }
                }
                columns
            }
            .frame(minHeight: 260)

            if store.showsRecurrentShelf {
                RecurrentShelfView(tasks: recurrent)
                    .frame(minHeight: 120, idealHeight: 200)
            }
        }
        .focusable()
        .focused($boardFocused)
        .focusEffectDisabled()
        .onKeyPress(phases: .down) { press in handle(press) }
        .onAppear { if selection == nil { selection = firstSelectableID() } }
        .sheet(item: Binding(get: { store.closeSheet },
                             set: { if $0 == nil { store.dismissCloseSheet() } })) { sheet in
            CloseSheetView(sheet: sheet).environment(store)
        }
        .toolbar {
            ToolbarItem(placement: .status) {
                BoardStatusLabel(taskCount: visibleTasks.count, roleFilter: roleFilter)
            }
            ToolbarItem {
                Toggle(isOn: $store.showsRecurrentShelf) {
                    Label("Recurrent Shelf", systemImage: "tray.2")
                }
                .help("Show or hide the recurrent task shelf")
                .keyboardShortcut("r", modifiers: [.command, .option])
            }
        }
    }

    private var columns: some View {
        HStack(alignment: .top, spacing: 0) {
            ForEach(Array(TaskStatus.boardColumns.enumerated()), id: \.offset) { index, status in
                BoardColumn(status: status,
                            tasks: column(status),
                            unfilteredTasks: store.boardTasks(status),
                            dragging: $dragging,
                            selection: $selection)
                if index < TaskStatus.boardColumns.count - 1 {
                    Divider()
                }
            }
        }
    }

    // MARK: - Keyboard parity

    /// Arrows move the selection; `⌘←`/`⌘→` move the selected **card**.
    ///
    /// The card move calls the same `Store.perform(drop:on:)` as a drag, so a
    /// keyboard user hits the same rules and the same confirmation sheet.
    private func handle(_ press: KeyPress) -> KeyPress.Result {
        let command = press.modifiers.contains(.command)
        switch press.key {
        case .leftArrow:
            return command ? moveSelectedCard(by: -1) : moveSelection(byColumn: -1)
        case .rightArrow:
            return command ? moveSelectedCard(by: 1) : moveSelection(byColumn: 1)
        case .upArrow:
            return moveSelection(byRow: -1)
        case .downArrow:
            return moveSelection(byRow: 1)
        default:
            return .ignored
        }
    }

    private func location(of id: String?) -> (column: Int, row: Int)? {
        guard let id else { return nil }
        for (index, status) in TaskStatus.boardColumns.enumerated() {
            if let row = column(status).firstIndex(where: { $0.id == id }) {
                return (index, row)
            }
        }
        return nil
    }

    private func firstSelectableID() -> String? {
        TaskStatus.boardColumns.lazy.compactMap { column($0).first?.id }.first
    }

    private func moveSelection(byColumn offset: Int) -> KeyPress.Result {
        guard let here = location(of: selection) else {
            selection = firstSelectableID()
            return selection == nil ? .ignored : .handled
        }
        var index = here.column + offset
        while TaskStatus.boardColumns.indices.contains(index) {
            let tasks = column(TaskStatus.boardColumns[index])
            if !tasks.isEmpty {
                selection = tasks[min(here.row, tasks.count - 1)].id
                return .handled
            }
            index += offset
        }
        return .handled
    }

    private func moveSelection(byRow offset: Int) -> KeyPress.Result {
        guard let here = location(of: selection) else {
            selection = firstSelectableID()
            return selection == nil ? .ignored : .handled
        }
        let tasks = column(TaskStatus.boardColumns[here.column])
        let row = max(0, min(tasks.count - 1, here.row + offset))
        selection = tasks[row].id
        return .handled
    }

    private func moveSelectedCard(by offset: Int) -> KeyPress.Result {
        guard let id = selection,
              let task = store.tasks.first(where: { $0.id == id }),
              let here = location(of: id) else { return .ignored }
        let source = TaskStatus.boardColumns[here.column]
        guard let target = store.neighbourColumn(of: source, offset: offset) else {
            store.show(.info("\(source.displayName) is the "
                             + (offset < 0 ? "first" : "last") + " column."))
            return .handled
        }
        let payload = TaskDragPayload(taskId: task.id, sourceStatus: store.effectiveStatus(of: task))
        _Concurrency.Task { await store.perform(drop: payload, on: target) }
        return .handled
    }
}

// MARK: - Feedback

/// The refused-drop / rolled-back-move bar. Transient, dismissible, and never a
/// modal: a refused drop is information, not an interruption.
private struct FeedbackBar: View {
    let feedback: BoardFeedback
    let dismiss: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: feedback.isError
                  ? "exclamationmark.triangle.fill" : "info.circle")
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(feedback.isError ? .orange : .secondary)
            VStack(alignment: .leading, spacing: 1) {
                Text(feedback.message).font(.callout)
                if let hint = feedback.hint {
                    Text(hint).font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 8)
            Button {
                dismiss()
            } label: {
                Image(systemName: "xmark").font(.caption)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Dismiss")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.5))
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isStaticText)
        .transition(.move(edge: .top).combined(with: .opacity))
    }
}

/// The count line in the toolbar, so the board's scope is never implicit.
private struct BoardStatusLabel: View {
    @Environment(Store.self) private var store
    let taskCount: Int
    let roleFilter: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(scopeText).font(.caption)
            if let updated = store.lastUpdated {
                Text("updated \(updated, style: .relative) ago")
                    .font(.caption2).foregroundStyle(.tertiary)
            }
        }
    }

    private var scopeText: String {
        let role = roleFilter.flatMap { id in
            store.roles.first { $0.id == id }?.displayName
        }
        let sprint = store.currentSprint?.displayName ?? "no current sprint"
        if let role { return "\(role) · \(taskCount) tasks · \(sprint)" }
        return "\(taskCount) tasks · \(sprint)"
    }
}

// MARK: - Column

/// One Kanban column: a header, a drop target, and a scrolling stack of cards.
struct BoardColumn: View {
    let status: TaskStatus
    /// What the current scope admits.
    let tasks: [TrackerTask]
    /// Everything in this column regardless of scope, so the header can show
    /// "3 of 12" rather than silently hiding nine cards. Phase 5's filters
    /// widen the gap; the role scope already opens it.
    let unfilteredTasks: [TrackerTask]
    @Binding var dragging: TaskDragPayload?
    @Binding var selection: String?

    @Environment(Store.self) private var store
    @State private var isTargeted = false
    @State private var hoverY: CGFloat?
    @State private var columnHeight: CGFloat = 0
    @State private var autoScrollRow: Int?

    private var totalMins: Double { tasks.reduce(0) { $0 + $1.reportableMins } }
    private var unfilteredMins: Double { unfilteredTasks.reduce(0) { $0 + $1.reportableMins } }

    /// Whether the card in flight can land here at all.
    private var accepts: Bool {
        guard let dragging else { return false }
        return BoardDropRules.accepts(dragging, in: status)
    }

    /// Where the insertion indicator goes, or `nil` when nothing is hovering.
    private var insertionRow: Int? {
        guard isTargeted, accepts, let dragging,
              dragging.status != status else { return nil }
        return store.landingIndex(of: dragging.taskId, movedTo: status)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            body(for: tasks)
        }
        .frame(minWidth: 260, maxWidth: .infinity, maxHeight: .infinity)
        .background {
            GeometryReader { proxy in
                Color.clear.onChange(of: proxy.size.height, initial: true) { _, height in
                    columnHeight = height
                }
            }
        }
        .overlay {
            // Green-lit or barred, but never ambiguous: an accepting column
            // glows, a refusing one is struck through with the reason.
            if isTargeted, dragging != nil {
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(accepts ? Color.accentColor : Color.orange,
                                  style: StrokeStyle(lineWidth: 2,
                                                     dash: accepts ? [] : [5, 4]))
                    .padding(3)
                    .allowsHitTesting(false)
            }
        }
        .overlay(alignment: .top) {
            if isTargeted, !accepts, let dragging {
                RefusalOverlay(rejection: rejection(for: dragging))
            }
        }
        // `.dropDestination`, not `.onDrop(of:delegate:)`.
        //
        // This is the bug Phase 4 shipped with: `.draggable` produces a
        // **Transferable** drag, `.onDrop(of:delegate:)` consumes an
        // **NSItemProvider** one, and the two do not interoperate. The drag
        // started and the card animated back, but no delegate callback ever
        // fired — verified with probes on a real drag: `dragStart` logged,
        // `validateDrop` / `dropEntered` / `performDrop` never did.
        //
        // `.dropDestination` is `.draggable`'s matching partner. It costs the
        // two things `ColumnDropDelegate` was chosen for — a synchronous
        // forbidden cursor, and a continuous hover location for spring-loaded
        // auto-scroll — but a refusal is still *visible*: the column keeps its
        // orange dashed border and `RefusalOverlay`, both driven by
        // `isTargeted` + `dragging`, and returning `false` snaps the card back.
        .dropDestination(for: TaskDragPayload.self) { items, _ in
            isTargeted = false
            hoverY = nil
            // Prefer the decoded payload over the shared `dragging` state: it
            // is what actually crossed the drag boundary.
            guard let payload = items.first ?? dragging else { return false }
            dragging = nil
            _Concurrency.Task { await store.perform(drop: payload, on: status) }
            // Refusals return false so the card animates home; `store.perform`
            // still runs, because it owns the "why" message.
            if case .rejected = BoardDropRules.decide(payload, to: status) { return false }
            return true
        } isTargeted: { targeted in
            isTargeted = targeted
            if !targeted { hoverY = nil }
        }
    }

    private func rejection(for payload: TaskDragPayload) -> BoardDropRejection? {
        if case .rejected(let why) = BoardDropRules.decide(payload, to: status) { return why }
        return nil
    }

    @ViewBuilder
    private func body(for tasks: [TrackerTask]) -> some View {
        if tasks.isEmpty {
            ZStack {
                ContentUnavailableView("Nothing in \(status.displayName)",
                                       systemImage: "tray")
                if insertionRow != nil { InsertionIndicator() .padding(.horizontal, 12) }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(Array(tasks.enumerated()), id: \.element.id) { index, task in
                            if insertionRow == index { InsertionIndicator() }
                            card(task)
                                .id(task.id)
                        }
                        if let insertionRow, insertionRow >= tasks.count {
                            InsertionIndicator()
                        }
                    }
                    .padding(10)
                    .animation(.snappy(duration: 0.18), value: insertionRow)
                }
                .onChange(of: hoverY) { _, y in autoScroll(to: y, proxy: proxy, tasks: tasks) }
                .onChange(of: isTargeted) { _, targeted in
                    if !targeted { autoScrollRow = nil }
                }
            }
        }
    }

    private func card(_ task: TrackerTask) -> some View {
        let payload = TaskDragPayload(taskId: task.id,
                                      sourceStatus: store.effectiveStatus(of: task))
        return TaskCardView(
            task: task,
            roleLabel: roleLabel(task),
            roleColor: RolePalette.color(forRoleID: task.roleId, in: store.roles),
            elapsed: elapsed(for: task),
            currentSprint: store.currentSprint,
            isSelected: selection == task.id,
            isPending: store.pendingStatus[task.id] != nil)
        .onTapGesture { selection = task.id }
        .contextMenu { menu(for: task) }
        // Recurrent cards are not draggable at all, so the prohibition is felt
        // before the drop rather than explained after it.
        .modifier(DraggableIfAllowed(payload: payload, dragging: $dragging))
    }

    @ViewBuilder
    private func menu(for task: TrackerTask) -> some View {
        ForEach(TaskStatus.boardColumns, id: \.rawValue) { target in
            let payload = TaskDragPayload(taskId: task.id,
                                          sourceStatus: store.effectiveStatus(of: task))
            if case .rejected = BoardDropRules.decide(payload, to: target) {
                EmptyView()
            } else {
                Button(target == .done ? "Close Task…" : "Move to \(target.displayName)") {
                    _Concurrency.Task { await store.perform(drop: payload, on: target) }
                }
            }
        }
    }

    /// Spring-loaded scrolling: hovering near a column's top or bottom edge
    /// walks the view a card at a time so a long column can be reached without
    /// letting go of the drag.
    private func autoScroll(to y: CGFloat?, proxy: ScrollViewProxy, tasks: [TrackerTask]) {
        guard let y, isTargeted, accepts, columnHeight > 0 else { return }
        let margin: CGFloat = 60
        let direction: Int
        if y < margin { direction = -1 }
        else if y > columnHeight - margin { direction = 1 }
        else { autoScrollRow = nil; return }

        let next = max(0, min(tasks.count - 1, (autoScrollRow ?? 0) + direction))
        guard next != autoScrollRow else { return }
        autoScrollRow = next
        withAnimation(.easeOut(duration: 0.2)) {
            proxy.scrollTo(tasks[next].id, anchor: direction < 0 ? .top : .bottom)
        }
    }

    private var header: some View {
        HStack {
            Text(status.displayName).font(.headline)
            CountBadge(shown: tasks.count, total: unfilteredTasks.count)
            Spacer()
            Text(Duration.formatZeroed(minutes: totalMins))
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
            if unfilteredMins > totalMins + 0.01 {
                Text("of \(Duration.formatZeroed(minutes: unfilteredMins))")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(status.displayName), \(tasks.count) of "
                            + "\(unfilteredTasks.count) tasks, "
                            + "\(Duration.formatZeroed(minutes: totalMins))")
    }

    private func roleLabel(_ task: TrackerTask) -> String {
        guard let id = task.roleId else { return "no role" }
        return store.roles.first { $0.id == id }?.displayName ?? id
    }

    private func elapsed(for task: TrackerTask) -> TimeInterval? {
        guard store.snapshot?.activeTimer?.taskId == task.id else { return nil }
        return store.activeTimerElapsed
    }
}

/// The column count, showing what is admitted **and** what exists.
///
/// Phase 5's filters are what make the two diverge in general, but the sidebar
/// role scope already does, and a header that showed only the filtered number
/// would let a stale scope hide work without saying so (plan risk #6).
private struct CountBadge: View {
    let shown: Int
    let total: Int

    var body: some View {
        HStack(spacing: 2) {
            Text("\(shown)")
            if shown != total {
                Text("of \(total)").foregroundStyle(.secondary)
            }
        }
        .font(.caption.monospacedDigit())
        .padding(.horizontal, 6).padding(.vertical, 1)
        .background(.quaternary, in: .capsule)
    }
}

/// The line showing where a dropped card will land.
private struct InsertionIndicator: View {
    var body: some View {
        Capsule()
            .fill(Color.accentColor)
            .frame(height: 3)
            .padding(.vertical, 2)
            .accessibilityHidden(true)
    }
}

/// The banner a refusing column shows while a card hovers over it, so the
/// "why not" arrives before the user lets go rather than after.
private struct RefusalOverlay: View {
    let rejection: BoardDropRejection?

    var body: some View {
        if let rejection {
            VStack(spacing: 2) {
                Text(rejection.message).font(.callout.weight(.medium))
                if let hint = rejection.hint {
                    Text(hint).font(.caption).multilineTextAlignment(.center)
                }
            }
            .padding(10)
            .frame(maxWidth: 260)
            .background(.regularMaterial, in: .rect(cornerRadius: 8))
            .overlay {
                RoundedRectangle(cornerRadius: 8).strokeBorder(.orange, lineWidth: 1)
            }
            .padding(.top, 44)
            .allowsHitTesting(false)
            .transition(.opacity)
        }
    }
}

/// `.draggable` only when the rules allow the card to be picked up.
///
/// The payload closure is where `dragging` is set: `.draggable(_:)` evaluates
/// it exactly when a drag session begins, which is the only synchronous hook
/// SwiftUI offers, and `DropDelegate.validateDrop` needs the value
/// synchronously.
private struct DraggableIfAllowed: ViewModifier {
    let payload: TaskDragPayload
    @Binding var dragging: TaskDragPayload?

    func body(content: Content) -> some View {
        if BoardDropRules.isDraggable(payload.status) {
            content.draggable({
                dragging = payload
                return payload
            }())
        } else {
            content
                .help("Recurrent tasks can’t be dragged — closing one ends the series.")
        }
    }
}

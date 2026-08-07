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

    /// What the column shows, after the shared `FilterState` (plan §8).
    private func column(_ status: TaskStatus) -> [TrackerTask] {
        store.filteredBoardTasks(status)
    }

    private var recurrent: [TrackerTask] { store.filteredRecurrentTasks }

    /// True when every board column is empty *because of* the filter — as
    /// opposed to a genuinely empty tracker, which is a different message.
    private var isFilteredToNothing: Bool {
        store.isFiltering
            && !store.tasks.isEmpty
            && TaskStatus.boardColumns.allSatisfy { column($0).isEmpty }
    }

    /// The keyboard cursor, built from **the same arrays the columns render**.
    ///
    /// `static` and internal so a test can construct it from a `Store` exactly
    /// the way the view does; that is what makes "a filtered-out card is not
    /// reachable by keyboard" an assertable fact rather than a claim.
    static func cursor(for store: Store) -> BoardCursor {
        BoardCursor(columns: TaskStatus.boardColumns.map {
            store.filteredBoardTasks($0).map(\.id)
        })
    }

    private var cursor: BoardCursor { Self.cursor(for: store) }

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
                // Sized to its rows rather than to a fixed guess — see
                // `RecurrentShelfView.naturalHeight`. `maxHeight` is what stops
                // `VSplitView` handing the shelf half the window and leaving a
                // band of empty table under the last row.
                //
                // `minHeight` must be the **same** value, not `min(height, 120)`
                // as it was through Phase 5. `VSplitView` resolves a subview to
                // its minimum whenever the other pane wants more room than is
                // available, and the board's Done column always does — 37 done
                // tasks against a finite window. Measured before this change:
                // seven rows needing 263pt were drawn 120pt tall, showing two of
                // them, in both an 800pt and an 1150pt window. Growing the window
                // did not help, which is what gave the minimum away.
                let shelfHeight = recurrent.isEmpty
                    ? RecurrentShelfView.emptyHeight
                    : RecurrentShelfView.naturalHeight(rows: recurrent.count)
                RecurrentShelfView(tasks: recurrent)
                    .frame(minHeight: shelfHeight,
                           idealHeight: shelfHeight,
                           maxHeight: shelfHeight)
            }
        }
        .focusable()
        .focused($boardFocused)
        .focusEffectDisabled()
        .onKeyPress(phases: .down) { press in handle(press) }
        .onAppear { selection = cursor.revalidated(selection) }
        // A filter change can pull the selected card out from under the cursor.
        // Revalidating here is what stops `⌘→` acting on an invisible card.
        .onChange(of: store.filter) { _, _ in selection = cursor.revalidated(selection) }
        .sheet(item: Binding(get: { store.closeSheet },
                             set: { if $0 == nil { store.dismissCloseSheet() } })) { sheet in
            CloseSheetView(sheet: sheet).environment(store)
        }
        // Phase 6's two shelf sheets. Attached here rather than inside
        // `RecurrentShelfView` so they survive the shelf being collapsed
        // mid-operation — a running reconcile must not lose its progress view
        // because ⌥⌘R was pressed.
        .sheet(item: Binding(get: { store.syncSheet },
                             set: { if $0 == nil { store.dismissSyncSheet() } })) { sheet in
            SyncSprintsSheetView(sheet: sheet).environment(store)
        }
        .sheet(item: Binding(get: { store.logSheet },
                             set: { if $0 == nil { store.dismissLogSheet() } })) { sheet in
            LogTimeSheetView(sheet: sheet).environment(store)
        }
        // The Task menu acts on the shelf row the menu bar can see.
        .focusedSceneValue(\.shelfTask, store.selectedShelfTask)
        .filterSearchField(store)
        .toolbar {
            ToolbarItem(placement: .status) {
                BoardStatusLabel(taskCount: store.filteredTasks.count,
                                 totalCount: store.tasks.count)
            }
            ToolbarItem { FilterMenu() }
            ToolbarItem {
                Toggle(isOn: $store.showsRecurrentShelf) {
                    Label("Recurrent Shelf", systemImage: "tray.2")
                }
                .help("Show or hide the recurrent task shelf")
                .keyboardShortcut("r", modifiers: [.command, .option])
            }
        }
    }

    @ViewBuilder
    private var columns: some View {
        if isFilteredToNothing {
            FilteredEmptyView()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            HStack(alignment: .top, spacing: 0) {
                ForEach(Array(TaskStatus.boardColumns.enumerated()),
                        id: \.offset) { index, status in
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

    private func moveSelection(byColumn offset: Int) -> KeyPress.Result {
        let cursor = cursor
        selection = cursor.move(from: selection, byColumn: offset)
        return selection == nil ? .ignored : .handled
    }

    private func moveSelection(byRow offset: Int) -> KeyPress.Result {
        let cursor = cursor
        selection = cursor.move(from: selection, byRow: offset)
        return selection == nil ? .ignored : .handled
    }

    /// `⌘←`/`⌘→`. Refuses outright when the selection is not on a **visible**
    /// card: `cursor.location` is built from the filtered columns, so a card the
    /// filter is hiding has no location and therefore cannot be moved.
    private func moveSelectedCard(by offset: Int) -> KeyPress.Result {
        guard let id = selection,
              let task = store.tasks.first(where: { $0.id == id }),
              let here = cursor.location(of: id) else { return .ignored }
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
    let totalCount: Int

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
        let sprint = store.currentSprint?.displayName ?? "no current sprint"
        let counts = taskCount == totalCount
            ? "\(taskCount) tasks"
            : "\(taskCount) of \(totalCount) tasks"
        return "\(counts) · \(sprint)"
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
                // A column emptied by the filter says so, and offers the way
                // out — the board-wide `FilteredEmptyView` only appears when
                // *all three* columns are empty.
                if unfilteredTasks.isEmpty {
                    ContentUnavailableView("Nothing in \(status.displayName)",
                                           systemImage: "tray")
                } else {
                    ContentUnavailableView {
                        Label("Nothing in \(status.displayName)", systemImage: "tray")
                    } description: {
                        Text("\(unfilteredTasks.count) hidden by the filter.")
                    } actions: {
                        Button("Clear Filters") { store.clearFilters() }
                    }
                }
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

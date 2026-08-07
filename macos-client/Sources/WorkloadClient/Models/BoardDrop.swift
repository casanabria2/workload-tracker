import CoreTransferable
import Foundation
import UniformTypeIdentifiers

// The Kanban drag payload and — more importantly — the **drop-rule table**.
//
// The table is a pure function over two statuses, in its own type, with no
// SwiftUI import in sight. That is deliberate: which transitions are permitted
// is the safety-critical logic of this phase (one of them runs `gh issue
// create` and `gh issue close` against the owner's real org), and safety-
// critical logic that can only be exercised by driving a UI is logic that does
// not get exercised.

// NOTE: there is deliberately no custom `UTType` here any more.
//
// The board originally used `UTType(exportedAs: "com.carlossanabria.workloadtracker.task")`.
// Drag-and-drop silently did not work: a card lifted, followed the pointer, and
// animated home, and **no drop destination callback ever fired** — with
// `.onDrop(of:delegate:)` and with `.dropDestination` alike.
//
// An `exportedAs:` type must be declared in the app's Info.plist under
// `UTExportedTypeDeclarations`. This target is a bare SwiftPM executable with no
// Info.plist and no bundle identifier, so the type was never registered, and
// drag routing — which resolves conformance through the system type registry —
// could never match it. Nothing warns you: `UTType.isDeclared` even answers
// `true` in-process.
//
// See `TaskDragPayload.contentType`. When the app is packaged as a real `.app`
// (plan §12 / Phase 9), a custom type can come back — declared properly.

/// What a dragged card carries.
///
/// The source status travels with the payload rather than being looked up at
/// drop time, so the rules can be evaluated against what the user actually
/// picked up even if a snapshot lands mid-drag.
struct TaskDragPayload: Codable, Transferable, Sendable, Equatable, Hashable {
    let taskId: String
    /// The raw status string, so an unknown status added on the Python side
    /// survives the round trip instead of being coerced to `todo`.
    let sourceStatus: String

    init(taskId: String, sourceStatus: TaskStatus) {
        self.taskId = taskId
        self.sourceStatus = sourceStatus.rawValue
    }

    var status: TaskStatus { TaskStatus(rawValue: sourceStatus) }

    /// The drag type. **Must be a system-registered type** — see the note at the
    /// top of this file. `.json` is honest about what a `CodableRepresentation`
    /// actually puts on the pasteboard.
    ///
    /// The cost of a public type is that a column will accept JSON dragged from
    /// anywhere; such a drop simply fails to decode and the card goes home.
    static let contentType: UTType = .json

    static var transferRepresentation: some TransferRepresentation {
        CodableRepresentation(contentType: Self.contentType)
    }
}

// MARK: - The rules

/// Why a drop was refused, with the copy the UI shows.
///
/// Each case corresponds to a row of plan §7's drop table. They are refusals of
/// *different* kinds and must not be collapsed into one "can't do that": the
/// reopen case is a missing feature, the recurrent case is a deliberate
/// prohibition.
enum BoardDropRejection: Equatable, Sendable {
    /// The card is already in that column.
    case sameColumn
    /// Dragging out of Done. `wt.py` has no reopen path — there is no
    /// `gh issue reopen` — and faking it locally would desync the GitHub
    /// Project.
    case reopenNotSupported
    /// A recurrent card, in either direction. Closing a recurrent task ends the
    /// series and closes its live issue; CLAUDE.md warns about this explicitly,
    /// and the only route is the shelf's own confirmed action.
    case recurrentLocked
    /// A status this build does not know about, in either position.
    case unknownStatus(String)

    var message: String {
        switch self {
        case .sameColumn:
            "Already there."
        case .reopenNotSupported:
            "Reopening isn’t supported."
        case .recurrentLocked:
            "Recurrent tasks can’t be dragged."
        case .unknownStatus(let raw):
            "“\(raw)” isn’t a board column."
        }
    }

    /// The "why not", shown under the message. `nil` where the message is
    /// self-explanatory.
    var hint: String? {
        switch self {
        case .sameColumn:
            nil
        case .reopenNotSupported:
            "The tracker has no reopen path (there is no `gh issue reopen` in wt.py), "
            + "and reopening locally would desync the GitHub Project."
        case .recurrentLocked:
            "Closing one ends the whole series and closes its live issue. "
            + "Use the recurrent shelf’s End Series action instead."
        case .unknownStatus:
            "Only To Do, In Progress and Done are drop targets."
        }
    }
}

/// What a drop should do.
enum BoardDropDecision: Equatable, Sendable {
    /// Do nothing but tell the user why.
    case rejected(BoardDropRejection)
    /// `POST /v1/tasks/{id}/status`. Applied to the UI immediately and rolled
    /// back if the daemon refuses.
    case optimisticStatus(TaskStatus)
    /// Open the §7.1 close sheet. **No request is issued by the drop itself**
    /// beyond the sheet's write-free `close/plan` dry run.
    case confirmClose
}

/// Plan §7's drop table, as a pure function.
///
/// | Drop | Behaviour |
/// |---|---|
/// | → In Progress | `POST /status {inprogress}`, optimistic |
/// | → To Do (from In Progress) | `POST /status {todo}`, optimistic |
/// | → Done | the confirmation sheet, never silent |
/// | → anywhere **from** Done | rejected, "reopening isn't supported" |
/// | recurrent → anywhere | rejected |
///
/// The asymmetry is not an oversight: the underlying operations are not
/// symmetric. Two of them are a one-field write; the third mints and closes
/// GitHub issues.
enum BoardDropRules {

    /// The whole table. Precedence matters and is asserted by the tests:
    /// recurrent beats everything (it is a prohibition, not a limitation);
    /// an unknown status is next, because nothing can be reasoned about it;
    /// a same-column drop is a no-op *before* it is a reopen attempt, so
    /// dropping a Done card back on Done says "already there" rather than
    /// lecturing about reopening.
    static func decide(from source: TaskStatus, to target: TaskStatus) -> BoardDropDecision {
        if source == .recurrent || target == .recurrent {
            return .rejected(.recurrentLocked)
        }
        if case .unknown(let raw) = source { return .rejected(.unknownStatus(raw)) }
        if case .unknown(let raw) = target { return .rejected(.unknownStatus(raw)) }
        if source == target { return .rejected(.sameColumn) }
        if source == .done { return .rejected(.reopenNotSupported) }
        if target == .done { return .confirmClose }
        return .optimisticStatus(target)
    }

    /// Convenience over a payload, for the drop handler.
    static func decide(_ payload: TaskDragPayload, to target: TaskStatus) -> BoardDropDecision {
        decide(from: payload.status, to: target)
    }

    /// Whether a card may be picked up at all. Recurrent cards are not
    /// draggable, so the refusal is felt before the drop rather than after —
    /// but `decide` still rejects them, because a payload can arrive from
    /// anywhere.
    static func isDraggable(_ status: TaskStatus) -> Bool {
        status != .recurrent
    }

    /// Whether *this* column will accept *this* card — what a drop target uses
    /// to decide whether to highlight and whether to show the "no" cursor.
    static func accepts(_ payload: TaskDragPayload, in column: TaskStatus) -> Bool {
        switch decide(payload, to: column) {
        case .rejected: false
        case .optimisticStatus, .confirmClose: true
        }
    }
}

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

// NOTE: the custom `UTType` is now **declared but not used**. Read this before
// changing `TaskDragPayload.contentType`.
//
// History: the board originally used
// `UTType(exportedAs: "com.carlossanabria.workloadtracker.task")` and
// drag-and-drop silently did not work — a card lifted, followed the pointer, and
// animated home, and **no drop destination callback ever fired**, with
// `.onDrop(of:delegate:)` and with `.dropDestination` alike. An `exportedAs:`
// type must be declared in the app's Info.plist under
// `UTExportedTypeDeclarations`; the target was a bare SwiftPM executable with no
// Info.plist and no bundle identifier, so the type was never registered and drag
// routing — which resolves conformance through the system type registry — could
// never match it. Nothing warns you: `UTType.isDeclared` even answers `true`
// in-process.
//
// What changed in Phase 9 (plan §12): `macos-client/Info.plist` now declares the
// type, `make-app.sh` builds a real `WorkloadTracker.app` around it, and
// `lsregister` registers it. Verified in the LaunchServices database:
//
//     type id: com.carlossanabria.workloadtracker.task
//     flags:   active  exported  untrusted
//     conforms to: public.data
//
// So the blocker is gone — but `contentType` is deliberately **still `.json`**:
//
//   1. A SwiftUI drag cannot be exercised by an automated test. Synthesised
//      mouse drags do not start one, which is exactly how the original bug
//      survived to ship. Flipping the type would trade a hand-verified working
//      feature for an unverifiable one.
//   2. The registration carries the `untrusted` flag, because the bundle is
//      ad-hoc signed rather than Developer-ID signed. Whether that matters to
//      drag conformance resolution is unknown, and unknowable without the manual
//      drag in (1).
//
// To switch (one line, and one manual test — see macos-client/README.md):
//
//     static let contentType = UTType(exportedAs: "com.carlossanabria.workloadtracker.task")
//
// then run the **bundled** app (not `swift run`) and drag a card from To Do to
// In Progress. If nothing moves, revert; do not "fix" the drop handlers.

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
    ///
    /// Since Phase 9 the app's Info.plist *does* declare
    /// `com.carlossanabria.workloadtracker.task`, so the custom type is a
    /// one-line switch away — kept out of the shipped default until someone
    /// performs the one manual drag that no test can perform for them.
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

/// Where a card was released, which is what separates "move it" from "put it
/// *there*".
///
/// Keeping the landing area in the payload — rather than inferring it from a
/// pointer coordinate at drop time — is what lets the rule table stay a pure
/// function that a test can drive without a drag session.
///
/// Note that **every drag expresses a placement**: a card zone or the empty
/// space below the cards, and nothing else is reachable with the mouse.
/// `.column` exists for the routes that have no pointer at all — `⌘←` / `⌘→`,
/// and any drop that arrives without a resolvable landing area.
enum BoardDropTarget: Equatable, Sendable {
    /// Released over the card with this id: insert *before* it, which is
    /// exactly where the insertion line was drawn.
    case before(taskId: String)
    /// Released below the last card: append. This is what the column's own
    /// empty space means, so releasing into the gap under a short column does
    /// the obvious thing rather than nothing.
    case end
    /// No placement expressed — a keyboard move. The card takes the status
    /// change alone and therefore arrives unpositioned, at the top of its new
    /// column (`wt_api.set_status` clears `position`).
    case column
}

/// What a drop should do.
enum BoardDropDecision: Equatable, Sendable {
    /// Do nothing but tell the user why.
    case rejected(BoardDropRejection)
    /// `POST /v1/tasks/{id}/status`, then — unless the placement is `.column` —
    /// `POST /v1/tasks/reorder` for the destination column. Applied to the UI
    /// immediately and rolled back if the daemon refuses.
    case optimisticStatus(TaskStatus, at: BoardDropTarget)
    /// `POST /v1/tasks/reorder` alone: the card stays in its column and only
    /// changes place. **No status is written**, so this is the one drop that
    /// touches nothing but a local integer.
    case reorder(BoardDropTarget)
    /// Open the §7.1 close sheet. **No request is issued by the drop itself**
    /// beyond the sheet's write-free `close/plan` dry run.
    ///
    /// Carries no placement: a task being closed is leaving the board, and
    /// `wt_api.close()` clears its position anyway.
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
///
/// Manual ordering adds a second axis — *where* in the column the card was
/// released (`BoardDropTarget`) — without adding a second table. The status
/// verdict is decided first and identically; the placement only rides along.
/// The one new row is the same-column drop, which used to be uniformly a shrug
/// and now depends on where it landed:
///
/// | Drop | Behaviour |
/// |---|---|
/// | same column, onto a card | `POST /tasks/reorder`, no status write |
/// | same column, onto the background | rejected, "already there" |
enum BoardDropRules {

    /// The whole table. Precedence matters and is asserted by the tests:
    /// recurrent beats everything (it is a prohibition, not a limitation);
    /// an unknown status is next, because nothing can be reasoned about it;
    /// a same-column drop is resolved *before* the Done rules, so dropping a
    /// Done card onto another Done card reorders it rather than lecturing about
    /// reopening — reordering Done is a pure local write and reaches no `gh`
    /// command at all.
    static func decide(from source: TaskStatus, to target: TaskStatus,
                       at placement: BoardDropTarget = .column) -> BoardDropDecision {
        if source == .recurrent || target == .recurrent {
            return .rejected(.recurrentLocked)
        }
        if case .unknown(let raw) = source { return .rejected(.unknownStatus(raw)) }
        if case .unknown(let raw) = target { return .rejected(.unknownStatus(raw)) }
        if source == target {
            // Dropping a card back on its own column's background asks for
            // nothing; dropping it on a specific card asks for a place.
            return placement == .column ? .rejected(.sameColumn) : .reorder(placement)
        }
        if source == .done { return .rejected(.reopenNotSupported) }
        if target == .done { return .confirmClose }
        return .optimisticStatus(target, at: placement)
    }

    /// Convenience over a payload, for the drop handler.
    static func decide(_ payload: TaskDragPayload, to target: TaskStatus,
                       at placement: BoardDropTarget = .column) -> BoardDropDecision {
        decide(from: payload.status, to: target, at: placement)
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
    ///
    /// Asks the question with a real placement (`.end`), not `.column`: a
    /// same-column drag is a legitimate reorder, and probing with `.column`
    /// would light the card's own column orange and put "Already there" over
    /// it for the whole of every reorder.
    static func accepts(_ payload: TaskDragPayload, in column: TaskStatus) -> Bool {
        switch decide(payload, to: column, at: .end) {
        case .rejected: false
        case .optimisticStatus, .reorder, .confirmClose: true
        }
    }
}

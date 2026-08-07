import Foundation

/// The board's keyboard cursor, over exactly the cards the board is rendering.
///
/// Extracted from `BoardView` in Phase 5 for one reason: with filtering on, "the
/// arrow keys can only reach a visible card" stops being obviously true and
/// becomes a property worth asserting. A cursor built from anything other than
/// the rendered arrays could walk onto a filtered-out card and then `⌘→` would
/// move a card the user cannot see.
///
/// `BoardView.cursor(for:)` is the only construction site, and it builds this
/// from the same `Store.filteredBoardTasks` call the columns render from — so
/// the test that drives this type is testing the real thing.
struct BoardCursor: Equatable, Sendable {
    /// Task ids per board column, in render order. Parallel to
    /// `TaskStatus.boardColumns`.
    let columns: [[String]]

    init(columns: [[String]]) {
        self.columns = columns
    }

    /// Every id the cursor can land on.
    var reachableIDs: Set<String> { Set(columns.flatMap { $0 }) }

    func contains(_ id: String) -> Bool { reachableIDs.contains(id) }

    var isEmpty: Bool { columns.allSatisfy(\.isEmpty) }

    func location(of id: String?) -> (column: Int, row: Int)? {
        guard let id else { return nil }
        for (index, ids) in columns.enumerated() {
            if let row = ids.firstIndex(of: id) { return (index, row) }
        }
        return nil
    }

    /// The first card the cursor should land on when nothing is selected.
    var firstID: String? { columns.lazy.compactMap(\.first).first }

    /// The selection to keep after the data changed: the current one if it is
    /// still on screen, otherwise the first card, otherwise nothing.
    ///
    /// This is what a filter change calls. Without it a card filtered out from
    /// under the selection would stay selected — invisible, and still the target
    /// of `⌘→`.
    func revalidated(_ id: String?) -> String? {
        if let id, contains(id) { return id }
        return firstID
    }

    /// Moves left/right, skipping empty columns and stopping at the ends.
    func move(from id: String?, byColumn offset: Int) -> String? {
        guard let here = location(of: id) else { return firstID }
        var index = here.column + offset
        while columns.indices.contains(index) {
            let ids = columns[index]
            if !ids.isEmpty { return ids[min(here.row, ids.count - 1)] }
            index += offset
        }
        return id
    }

    /// Moves up/down within a column, clamped at its ends.
    func move(from id: String?, byRow offset: Int) -> String? {
        guard let here = location(of: id) else { return firstID }
        let ids = columns[here.column]
        guard !ids.isEmpty else { return id }
        return ids[max(0, min(ids.count - 1, here.row + offset))]
    }
}

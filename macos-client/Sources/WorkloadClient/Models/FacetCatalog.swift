import Foundation

// Plan §8.3 — the facet options, derived from the snapshot rather than from a
// static list, so the bar never offers a value that matches nothing.

/// One selectable value in a facet.
struct FacetOption: Identifiable, Hashable, Sendable {
    /// What goes into `FilterState`; may be `FilterState.unset`.
    let value: String
    /// What the user reads.
    let label: String
    /// How many tasks selecting **only** this option would admit. Computed
    /// through `TaskFilter` itself, so the number in the menu can never drift
    /// from the number of cards that appear — including the zero-log exemption
    /// inflating the current sprint's count above its `sprints_with_time` tally.
    let count: Int

    var id: String { value }
}

/// Every facet's options, with the self-hide rule applied.
struct FacetCatalog: Equatable, Sendable {
    private let byFacet: [Facet: [FacetOption]]

    static let empty = FacetCatalog(byFacet: [:])

    private init(byFacet: [Facet: [FacetOption]]) {
        self.byFacet = byFacet
    }

    // MARK: - Building

    /// Derives the options from the snapshot's tasks.
    ///
    /// Counts include recurrent tasks: the filter is shared with the shelf and
    /// (from Phase 7) the timeline, so a catalog scoped to the three board
    /// columns would under-report by 7 and change meaning per view.
    static func build(from snapshot: Snapshot?) -> FacetCatalog {
        guard let snapshot, !snapshot.tasks.isEmpty else { return .empty }
        let tasks = snapshot.tasks
        let currentSprintID = snapshot.currentSprint?.id

        func count(_ facet: Facet, _ value: String) -> Int {
            let probe = FilterState(facet: facet, value: value)
            return tasks.count {
                TaskFilter.matches($0, probe, currentSprintID: currentSprintID)
            }
        }

        var byFacet: [Facet: [FacetOption]] = [:]

        // Role — snapshot order, which is the sidebar's order, so the two lists
        // read the same way. Roles with no tasks are omitted: the sidebar is a
        // directory of what exists, the filter menu is a list of what subtracts.
        let roleLabels = Dictionary(snapshot.roles.map { ($0.id, $0.displayName) },
                                    uniquingKeysWith: { first, _ in first })
        var roleOptions: [FacetOption] = []
        for role in snapshot.roles where tasks.contains(where: { $0.roleId == role.id }) {
            roleOptions.append(FacetOption(value: role.id,
                                           label: role.displayName,
                                           count: count(.role, role.id)))
        }
        // A task can carry a role id the roles list does not define, and a task
        // can carry none at all. Neither may become unreachable.
        let knownRoles = Set(snapshot.roles.map(\.id))
        for orphan in Set(tasks.compactMap(\.roleId)).subtracting(knownRoles).sorted() {
            roleOptions.append(FacetOption(value: orphan,
                                           label: roleLabels[orphan] ?? orphan,
                                           count: count(.role, orphan)))
        }
        if tasks.contains(where: { TaskFilter.value(of: $0, in: .role) == FilterState.unset }) {
            roleOptions.append(FacetOption(value: FilterState.unset,
                                           label: "No Role",
                                           count: count(.role, FilterState.unset)))
        }
        byFacet[.role] = roleOptions

        // Activity Type and Repository — most-used first, "no value" last.
        byFacet[.activityType] = singleValued(.activityType, in: tasks,
                                              unsetLabel: "No Activity", count: count)
        byFacet[.repository] = singleValued(.repository, in: tasks,
                                            unsetLabel: "No Repository", count: count)

        // Sprint — only the sprints with logged time (11 of the owner's 72
        // cached), newest first.
        var sprintTitles: [String: String] = [:]
        var sprintStarts: [String: String] = [:]
        for sprint in snapshot.sprints {
            sprintTitles[sprint.id] = sprint.displayName
            sprintStarts[sprint.id] = sprint.startDate ?? ""
        }
        var worked: Set<String> = []
        for task in tasks {
            for entry in task.sprintsWithTime {
                guard let id = entry.sprintId else { continue }
                worked.insert(id)
                // A sprint dropped from the cache still has to render as
                // something other than a hex id.
                if sprintTitles[id] == nil { sprintTitles[id] = entry.sprintTitle ?? id }
                if sprintStarts[id] == nil { sprintStarts[id] = entry.startDate ?? "" }
            }
        }
        byFacet[.sprint] = worked
            .sorted {
                let l = sprintStarts[$0] ?? "", r = sprintStarts[$1] ?? ""
                return l == r ? $0 < $1 : l > r
            }
            .map { FacetOption(value: $0,
                               label: sprintTitles[$0] ?? $0,
                               count: count(.sprint, $0)) }

        return FacetCatalog(byFacet: byFacet)
    }

    private static func singleValued(_ facet: Facet,
                                     in tasks: [TrackerTask],
                                     unsetLabel: String,
                                     count: (Facet, String) -> Int) -> [FacetOption] {
        var tally: [String: Int] = [:]
        for task in tasks { tally[TaskFilter.value(of: task, in: facet), default: 0] += 1 }
        let named = tally.keys.filter { $0 != FilterState.unset }
            .sorted { tally[$0]! == tally[$1]! ? $0 < $1 : tally[$0]! > tally[$1]! }
            .map { FacetOption(value: $0, label: $0, count: count(facet, $0)) }
        guard tally[FilterState.unset] != nil else { return named }
        return named + [FacetOption(value: FilterState.unset,
                                    label: unsetLabel,
                                    count: count(facet, FilterState.unset))]
    }

    // MARK: - Reading

    func options(for facet: Facet) -> [FacetOption] { byFacet[facet] ?? [] }

    /// Plan §8.3's self-hide rule: **a facet offering fewer than 2 distinct
    /// values is hidden entirely**, because a control that cannot change the
    /// result is worse than no control.
    func isVisible(_ facet: Facet) -> Bool { options(for: facet).count >= 2 }

    var visibleFacets: [Facet] { Facet.allCases.filter(isVisible) }

    /// The user-facing name for a stored value — the only way a token or an
    /// accessibility label should ever render one, since `FilterState.unset` is
    /// a control character.
    func label(for value: String, in facet: Facet) -> String {
        options(for: facet).first { $0.value == value }?.label
            ?? (value == FilterState.unset ? "None" : value)
    }
}

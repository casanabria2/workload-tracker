import Foundation

// Plan §8 — filtering, shared by every view of the snapshot.
//
// Everything in this file is pure and SwiftUI-free, for the same reason
// `BoardDrop.swift` is: the combination rule (OR within a facet, AND across
// facets) and the Sprint facet's logged-time semantics are the substance of the
// phase, and logic that can only be exercised by driving a UI is logic that does
// not get exercised.

// MARK: - Facets

/// The four filter facets (plan §8.1).
///
/// **`type` is deliberately not here.** All 55 of the owner's tasks have it
/// unset and the GitHub Project's Type option list is empty, so it is not a
/// facet that could ever subtract anything. `FacetCatalog`'s self-hide rule
/// would drop it anyway; leaving it out of the enum makes that a fact about the
/// code rather than a fact about today's data.
enum Facet: String, CaseIterable, Codable, Sendable, Hashable, Identifiable {
    case role
    case activityType
    case repository
    case sprint

    var id: String { rawValue }

    /// Singular, because it labels one submenu and prefixes one token.
    var displayName: String {
        switch self {
        case .role: "Role"
        case .activityType: "Activity Type"
        case .repository: "Repository"
        case .sprint: "Sprint"
        }
    }

    var systemImage: String {
        switch self {
        case .role: "person.2"
        case .activityType: "tag"
        case .repository: "shippingbox"
        case .sprint: "calendar"
        }
    }
}

// MARK: - The state

/// What the user has filtered to. One instance, shared by the sidebar, the
/// filter bar, the board and (from Phase 7) the timeline — plan §8.1 is explicit
/// that these are two views of one state, not two states.
///
/// Persisted verbatim in `@SceneStorage` as JSON, so it must stay `Codable` and
/// must tolerate decoding a payload written by an older build (every property
/// has a default and `init(from:)` decodes each one optionally).
struct FilterState: Codable, Equatable, Sendable {
    var roles: Set<String> = []
    var activityTypes: Set<String> = []
    var repos: Set<String> = []
    /// Sprint **ids**, matched by logged time (§8.2), never by the legacy
    /// `sprint_id` field.
    var sprints: Set<String> = []
    var text: String = ""

    init(roles: Set<String> = [], activityTypes: Set<String> = [],
         repos: Set<String> = [], sprints: Set<String> = [], text: String = "") {
        self.roles = roles
        self.activityTypes = activityTypes
        self.repos = repos
        self.sprints = sprints
        self.text = text
    }

    /// One facet selected, everything else open. The shape `FacetCatalog` uses
    /// to count an option, so a menu count always equals what picking it yields.
    init(facet: Facet, value: String) {
        self.init()
        self[facet] = [value]
    }

    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        roles = try c.decodeIfPresent(Set<String>.self, forKey: .roles) ?? []
        activityTypes = try c.decodeIfPresent(Set<String>.self, forKey: .activityTypes) ?? []
        repos = try c.decodeIfPresent(Set<String>.self, forKey: .repos) ?? []
        sprints = try c.decodeIfPresent(Set<String>.self, forKey: .sprints) ?? []
        text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
    }

    /// The value standing for "this task has no value for this facet".
    ///
    /// One task has no `activity`, and a task can have no role or no repo. Under
    /// a plain string match such a task is unreachable from the facet — it can
    /// only ever be excluded, never selected — so the catalog offers it as an
    /// explicit option. The control character guarantees it cannot collide with
    /// a real GitHub Project option name, a role id or a repo slug.
    static let unset = "\u{1}unset"

    subscript(facet: Facet) -> Set<String> {
        get {
            switch facet {
            case .role: roles
            case .activityType: activityTypes
            case .repository: repos
            case .sprint: sprints
            }
        }
        set {
            switch facet {
            case .role: roles = newValue
            case .activityType: activityTypes = newValue
            case .repository: repos = newValue
            case .sprint: sprints = newValue
            }
        }
    }

    /// True when nothing is constrained — every task passes.
    var isEmpty: Bool {
        Facet.allCases.allSatisfy { self[$0].isEmpty }
            && text.trimmingCharacters(in: .whitespaces).isEmpty
    }

    /// The facets carrying at least one value, in menu order. Free text is not a
    /// facet and is reported separately.
    var activeFacets: [Facet] { Facet.allCases.filter { !self[$0].isEmpty } }

    /// How many individual constraints are on, for the toolbar badge. Free text
    /// counts as one.
    var activeCount: Int {
        Facet.allCases.reduce(0) { $0 + self[$1].count }
            + (text.trimmingCharacters(in: .whitespaces).isEmpty ? 0 : 1)
    }

    mutating func toggle(_ value: String, in facet: Facet) {
        var values = self[facet]
        if values.contains(value) { values.remove(value) } else { values.insert(value) }
        self[facet] = values
    }
}

// MARK: - The filter

/// Plan §8's combination rule, as a pure function.
///
/// **OR within a facet, AND across facets.** `roles: {a, b}` + `sprints: {105}`
/// means *(a OR b) AND worked-in-105*. An empty facet is "no constraint", not
/// "match nothing" — the alternative makes multi-select useless and is the
/// single most likely thing to get wrong.
enum TaskFilter {

    /// `currentSprintID` is not part of `FilterState` on purpose: it is a fact
    /// about the data, not about what the user chose, and persisting it would
    /// freeze a two-week-lived value into `@SceneStorage`. It is required by the
    /// zero-log exemption (§8.2) and by nothing else — pass `nil` and a
    /// never-logged task simply fails a non-empty Sprint facet.
    static func apply(_ state: FilterState,
                      to tasks: [TrackerTask],
                      currentSprintID: String?) -> [TrackerTask] {
        guard !state.isEmpty else { return tasks }
        return tasks.filter { matches($0, state, currentSprintID: currentSprintID) }
    }

    /// The shelf's filter: **every facet except Sprint** (plan §9).
    ///
    /// A perpetual task accrues time in every sprint it runs through, so a
    /// logged-time Sprint facet applied here would either match all of them —
    /// making the facet pointless on the shelf — or hide the ones that happened
    /// to be quiet, and those are precisely the tasks meant to stay visible on
    /// launch.
    ///
    /// Phase 5 did **not** do this: `Store.filteredRecurrentTasks` ran the full
    /// `apply(...)`. It looked correct because §8.2's open-work exemption
    /// rescues a `recurrent` task whenever the *current* sprint is selected, and
    /// the current sprint is the default. Select a past sprint on its own —
    /// Sprint 100, say — and the shelf emptied: measured on the owner's data,
    /// 7 of 7 rows disappeared, because the exemption requires
    /// `selected.contains(currentSprintID)`. Ignoring the facet outright is what
    /// §9 specifies and is not the same rule.
    ///
    /// The "This sprint" *column* still respects the selection, so the facet
    /// changes what you read on the shelf — just not which rows exist.
    static func applyToShelf(_ state: FilterState,
                             to tasks: [TrackerTask],
                             currentSprintID: String?) -> [TrackerTask] {
        apply(ignoringSprints(state), to: tasks, currentSprintID: currentSprintID)
    }

    /// `state` with the Sprint facet cleared. Exposed so a test can assert the
    /// shelf and the board are given genuinely different filters.
    static func ignoringSprints(_ state: FilterState) -> FilterState {
        var relaxed = state
        relaxed.sprints = []
        return relaxed
    }

    static func matches(_ task: TrackerTask,
                        _ state: FilterState,
                        currentSprintID: String?) -> Bool {
        guard state.roles.isEmpty
                || state.roles.contains(value(of: task, in: .role)) else { return false }
        guard state.activityTypes.isEmpty
                || state.activityTypes.contains(value(of: task, in: .activityType))
        else { return false }
        guard state.repos.isEmpty
                || state.repos.contains(value(of: task, in: .repository)) else { return false }
        guard matchesSprints(task, state.sprints, currentSprintID: currentSprintID)
        else { return false }
        return matchesText(task, state.text)
    }

    /// The single-valued facets. Extracted so `FacetCatalog` derives its options
    /// from exactly the values the filter will later compare against.
    static func value(of task: TrackerTask, in facet: Facet) -> String {
        let raw: String? = switch facet {
        case .role: task.roleId
        case .activityType: task.activity
        case .repository: task.githubRepo
        case .sprint: nil  // multi-valued; see `sprintIDs(of:)`
        }
        guard let raw, !raw.isEmpty else { return FilterState.unset }
        return raw
    }

    /// The sprints a task has **logged time** in (§8.2) — read straight off
    /// `sprints_with_time`, which Python already bucketed by log timestamp with
    /// zero-minute sprints dropped. Swift never re-derives sprint attribution.
    static func sprintIDs(of task: TrackerTask) -> Set<String> {
        Set(task.sprintsWithTime.compactMap(\.sprintId))
    }

    /// The Sprint facet, including **the zero-log exemption**.
    ///
    /// 5 of the owner's 55 tasks have no logs and 3 of those are live To Do
    /// cards. Under a strict logged-time rule a card you just created would be
    /// invisible in the default (current-sprint) view. So: *a task with no
    /// logged time matches whenever the current sprint is among the selected
    /// sprints.* It still correctly disappears when you filter to a past sprint,
    /// because it was not worked then.
    static func matchesSprints(_ task: TrackerTask,
                               _ selected: Set<String>,
                               currentSprintID: String?) -> Bool {
        guard !selected.isEmpty else { return true }
        let worked = sprintIDs(of: task)
        if !worked.isDisjoint(with: selected) { return true }

        // The open-work exemption.
        //
        // Sprint means "has logged time in that sprint", which is right for the
        // Done column — that is the whole reason the facet exists, since 37 of
        // 55 tasks are done and an unscoped Done column is unreadable. Applied
        // to *open* work it is wrong: a task in flight that you have not logged
        // against this fortnight is exactly the task you most want on the board.
        //
        // Measured on the real data before this rule existed: the default
        // current-sprint filter hid **5 of the 6 In Progress cards**.
        //
        // So any non-`done` task with no time in the selected sprints stays
        // visible while the *current* sprint is one of them. Selecting only past
        // sprints still excludes it — it genuinely was not worked then. This
        // subsumes the narrower zero-log exemption the plan originally specified
        // (a brand-new task is just the special case with no logs at all).
        guard let currentSprintID, selected.contains(currentSprintID) else { return false }

        // Open work: always exempt.
        if task.status != .done { return true }

        // A *done* task is exempt only when it has no logged time at all. It has
        // no sprint anywhere, so the current sprint is the only place it can
        // sensibly appear. A done task that *does* have time belongs to the
        // sprints it was worked in and must not leak into every other one —
        // otherwise the Done column is unscoped again, which is the problem the
        // Sprint facet exists to solve.
        return worked.isEmpty
    }

    /// Free text over the title, the description and the current issue ref, so
    /// `#412` and `repo-a` both find their task.
    static func matchesText(_ task: TrackerTask, _ text: String) -> Bool {
        let needle = text.trimmingCharacters(in: .whitespaces)
        guard !needle.isEmpty else { return true }
        for haystack in [task.title, task.description, task.currentIssue ?? ""]
        where haystack.localizedCaseInsensitiveContains(needle) {
            return true
        }
        return false
    }

    // MARK: - Diagnosing an empty result

    /// Which facets are responsible for an empty result, so the empty state can
    /// name them instead of shrugging (§8.4).
    ///
    /// A facet is "blocking" when clearing **it alone** would admit something.
    /// When no single facet unblocks — two facets that are individually
    /// satisfiable but jointly impossible — every active facet is returned,
    /// because at that point there is no smaller true answer.
    static func blockingFacets(_ state: FilterState,
                               tasks: [TrackerTask],
                               currentSprintID: String?) -> [Facet] {
        guard apply(state, to: tasks, currentSprintID: currentSprintID).isEmpty else { return [] }
        let active = state.activeFacets
        let unblocking = active.filter { facet in
            var relaxed = state
            relaxed[facet] = []
            return !apply(relaxed, to: tasks, currentSprintID: currentSprintID).isEmpty
        }
        return unblocking.isEmpty ? active : unblocking
    }

    /// Whether dropping the free text alone would admit something. Reported
    /// separately from `blockingFacets` because the search field is a different
    /// control from the facet menu.
    static func textIsBlocking(_ state: FilterState,
                               tasks: [TrackerTask],
                               currentSprintID: String?) -> Bool {
        guard !state.text.trimmingCharacters(in: .whitespaces).isEmpty,
              apply(state, to: tasks, currentSprintID: currentSprintID).isEmpty else { return false }
        var relaxed = state
        relaxed.text = ""
        return !apply(relaxed, to: tasks, currentSprintID: currentSprintID).isEmpty
    }
}

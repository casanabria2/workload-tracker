import SwiftUI

// Plan §8.4 — two coordinated surfaces over one `FilterState`, which is the
// Finder/Mail idiom rather than a bespoke filter panel:
//
//   1. `.searchable` with tokens — free text plus one removable chip per active
//      facet value, so what is filtered is impossible to miss.
//   2. A "Filter" toolbar menu — one submenu per *visible* facet, multi-select
//      toggles with per-value counts.
//
// The sidebar's Roles rows are the third surface and write the same state.
// None of these owns anything: every one of them calls `Store.toggle` or
// `Store.applyTokens`.

/// The toolbar's funnel menu.
struct FilterMenu: View {
    @Environment(Store.self) private var store

    var body: some View {
        Menu {
            let visible = store.facets.visibleFacets
            if visible.isEmpty {
                Text("No filters available")
            }
            ForEach(visible) { facet in
                Menu {
                    FacetOptionList(facet: facet)
                } label: {
                    Label(facet.displayName, systemImage: facet.systemImage)
                }
            }
            Divider()
            Button("Clear All Filters") { store.clearFilters() }
                .disabled(!store.isFiltering)
        } label: {
            Label(label, systemImage: symbol)
        }
        // A toolbar `Menu` renders its `Label` icon-only by default, which threw
        // away the active-count badge §8.4 asks for — the first build of this
        // showed a funnel and no number. Forcing the title back puts the count
        // on screen.
        .labelStyle(.titleAndIcon)
        .menuIndicator(.hidden)
        .help(store.isFiltering
              ? "Filtering by \(store.filter.activeCount) value"
                + (store.filter.activeCount == 1 ? "" : "s")
              : "Filter tasks by role, activity, repository or sprint")
    }

    /// The badge plan §8.4 asks for. A count in the label rather than a dot,
    /// because a bare dot says "something is on" without saying how much.
    private var label: String {
        store.isFiltering ? "Filter (\(store.filter.activeCount))" : "Filter"
    }

    private var symbol: String {
        store.isFiltering
            ? "line.3.horizontal.decrease.circle.fill"
            : "line.3.horizontal.decrease.circle"
    }
}

/// One facet's multi-select rows, plus a clear for that facet alone.
private struct FacetOptionList: View {
    @Environment(Store.self) private var store
    let facet: Facet

    var body: some View {
        ForEach(store.facets.options(for: facet)) { option in
            Toggle(isOn: Binding(
                get: { store.isSelected(option.value, in: facet) },
                set: { _ in store.toggle(option.value, in: facet) })) {
                    Text("\(option.label)  (\(option.count))")
                }
        }
        Divider()
        Button("Clear \(facet.displayName)") { store.clear(facet) }
            .disabled(store.filter[facet].isEmpty)
    }
}

// MARK: - Searchable tokens

extension View {
    /// Attaches the §8.4 search field: free text bound to `FilterState.text`,
    /// and one token per active facet value.
    ///
    /// The token array is a **projection** of `FilterState`, written back
    /// wholesale on every edit. That is what makes deleting a chip uncheck the
    /// sidebar row and the menu item — there is only ever one state.
    func filterSearchField(_ store: Store) -> some View {
        modifier(FilterSearchField(store: store))
    }
}

private struct FilterSearchField: ViewModifier {
    @Bindable var store: Store
    /// Driven by **Edit ▸ Find** (`⌘F`). `.searchFocused` is the only supported
    /// way to move focus into a `.searchable` field from outside it; a menu
    /// command cannot reach the field directly.
    @FocusState private var searchFocused: Bool

    func body(content: Content) -> some View {
        content.searchable(
            text: $store.filter.text,
            tokens: Binding(get: { store.filterTokens },
                            set: { store.applyTokens($0) }),
            placement: .toolbar,
            prompt: Text("Filter tasks")
        ) { token in
            // The facet is carried by its SF Symbol rather than spelled out.
            // `Text(token.display)` was the first cut and it read correctly, but
            // two chips (`Role: Managing DemoKit` + `Sprint 102`) already
            // overran the toolbar's search field and the second was clipped
            // mid-word. The icon keeps the same information in a third of the
            // width; `token.display` survives as the accessibility label.
            // **The chip is the bare value, not `token.display`.**
            //
            // Plan §8.4 asks for `Activity Type: Demo Kit Maintenance` chips.
            // Measured on screen: at the default window width the toolbar's
            // search field fits roughly one such chip, and a second
            // (`Role: Managing DemoKit` + `Sprint 105`) was clipped mid-word.
            // Two attempts to carry the facet compactly both failed — an
            // `Image` interpolated into the `Text`, and a
            // `Label(_:systemImage:)` with `.titleAndIcon` — because a macOS
            // search token renders its content as plain text and drops
            // everything else.
            //
            // So the chip shows the value in full (which is the part that
            // identifies it), the facet lives in the accessibility label, and
            // the Filter menu remains the authoritative, fully-labelled view of
            // what is on. A packaged `.app` with a wider default window (Phase
            // 9) can revisit this.
            Text(token.label)
                .accessibilityLabel(token.display)
        }
        .searchFocused($searchFocused)
        // A counter, not a flag: pressing ⌘F twice must focus twice, and a
        // `Bool` already `true` produces no `onChange`.
        .onChange(of: store.searchFocusRequests) { _, _ in searchFocused = true }
    }
}

// MARK: - Empty state

/// What a view shows when the filters admit nothing (§8.4: "never a blank
/// pane"). Names the facets actually responsible and clears them in one click.
struct FilteredEmptyView: View {
    @Environment(Store.self) private var store

    var body: some View {
        let blocking = store.blockingFacets
        let text = store.textIsBlocking
        ContentUnavailableView {
            Label("No tasks match the filter", systemImage: "line.3.horizontal.decrease.circle")
        } description: {
            Text(explanation(blocking: blocking, textIsBlocking: text))
        } actions: {
            VStack(spacing: 8) {
                ForEach(blocking) { facet in
                    Button("Clear \(facet.displayName)") { store.clear(facet) }
                }
                if text {
                    Button("Clear Search Text") { store.filter.text = "" }
                }
                Button("Clear All Filters") { store.clearFilters() }
                    .keyboardShortcut(.defaultAction)
            }
        }
    }

    private func explanation(blocking: [Facet], textIsBlocking: Bool) -> String {
        var parts = blocking.map(\.displayName)
        if textIsBlocking { parts.append("the search text") }
        guard !parts.isEmpty else {
            return "\(store.tasks.count) tasks exist, but none of them pass the "
                + "current filter."
        }
        let list = ListFormatter.localizedString(byJoining: parts)
        return "Relaxing \(list) would bring tasks back. "
            + "\(store.tasks.count) tasks exist in total."
    }
}

import SwiftUI
import AppKit

/// Diagonal hatching for the Gantt's approximately-placed bars (plan §10).
///
/// The 29 timestamp-less logs carry `at` — when the entry was recorded — but no
/// wall clock for the work itself. They are drawn at `log_effective_date` with a
/// width of `minutes`, and **hatched**, so the imprecision is visible rather
/// than smuggled into a solid bar that looks as exact as its neighbours.
///
/// Two constraints shaped the implementation:
///
/// 1. **It has to be a `ShapeStyle`**, because that is what
///    `ChartContent.foregroundStyle(_:)` takes. `ImagePaint` is the only
///    built-in style that tiles an arbitrary drawing, so the hatch is a small
///    `NSImage` tile.
/// 2. **It has to be tinted per role**, since role colour is the other channel
///    on the same bar. So a tile is generated per colour and cached; the colours
///    come from `RolePalette`, so the cache stays at ~10 entries.
///
/// Colour is never the only channel: the hatch has a legend entry spelling out
/// "approximate time of day", and every approximate bar's accessibility label
/// says so too.
@MainActor
enum HatchPattern {

    /// Tile edge, in points. Small enough that a 30-minute bar at Day zoom still
    /// shows several stripes.
    private static let tileSize: CGFloat = 6

    private static var cache: [String: ImagePaint] = [:]

    /// A tiling diagonal hatch in `color`, for use as a mark's foreground style.
    static func paint(_ color: Color) -> ImagePaint {
        let base = NSColor(color)
        let key = key(for: base)
        if let cached = cache[key] { return cached }
        let paint = ImagePaint(image: Image(nsImage: tile(base)), scale: 1)
        cache[key] = paint
        return paint
    }

    /// The tile: a washed-out ground in the role colour with opaque diagonal
    /// stripes over it, so the bar still reads as belonging to its role.
    private static func tile(_ color: NSColor) -> NSImage {
        let size = NSSize(width: tileSize, height: tileSize)
        let image = NSImage(size: size)
        image.lockFocus()
        defer { image.unlockFocus() }

        color.withAlphaComponent(0.22).setFill()
        NSRect(origin: .zero, size: size).fill()

        color.withAlphaComponent(0.95).setStroke()
        let path = NSBezierPath()
        path.lineWidth = 1.6
        // Two strokes, offset by the tile size, so the diagonal continues across
        // the seam when the image tiles.
        for offset in [CGFloat(0), tileSize] {
            path.move(to: NSPoint(x: -offset, y: 0))
            path.line(to: NSPoint(x: tileSize - offset, y: tileSize))
        }
        path.stroke()
        return image
    }

    /// A stable key for an `NSColor`. Resolved into sRGB first: two `Color`s can
    /// be equal and yet produce `NSColor`s in different colour spaces, and a
    /// dynamic system colour has no components at all until it is resolved.
    private static func key(for color: NSColor) -> String {
        guard let rgb = color.usingColorSpace(.sRGB) else { return color.description }
        return String(format: "%.3f,%.3f,%.3f,%.3f",
                      rgb.redComponent, rgb.greenComponent,
                      rgb.blueComponent, rgb.alphaComponent)
    }
}

/// The legend's hatch swatch — the same drawing, at chip size.
///
/// Drawn with SwiftUI shapes rather than the `ImagePaint` tile so it stays crisp
/// at 22×12 and needs no colour resolution; it only ever has to *look* like the
/// bars, and it always carries its label.
struct HatchSwatch: View {
    var color: Color = .secondary

    var body: some View {
        RoundedRectangle(cornerRadius: 2)
            .fill(color.opacity(0.22))
            .overlay {
                Canvas { context, size in
                    var path = Path()
                    var x: CGFloat = -size.height
                    while x < size.width {
                        path.move(to: CGPoint(x: x, y: size.height))
                        path.addLine(to: CGPoint(x: x + size.height, y: 0))
                        x += 4
                    }
                    context.stroke(path, with: .color(color.opacity(0.95)),
                                   lineWidth: 1.4)
                }
                .clipShape(RoundedRectangle(cornerRadius: 2))
            }
            .overlay {
                RoundedRectangle(cornerRadius: 2)
                    .strokeBorder(color.opacity(0.5), lineWidth: 0.5)
            }
    }
}

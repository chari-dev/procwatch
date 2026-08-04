// A menu bar item that shows the procwatch dashboard, unchanged, in a panel.
//
// The dashboard is already a web page served on localhost, so this deliberately
// adds nothing to it: no native reimplementation of the charts, no second copy
// of the layout to keep in sync. It is a status item, a web view, and the code
// to make sure the server behind it is running.
//
// Built with system frameworks only (AppKit, WebKit), matching the rest of the
// project's no-third-party-dependencies rule.

import AppKit
import IOKit.pwr_mgt
import Network
import ServiceManagement
import UserNotifications
import WebKit

let defaultPort = 8790

/// Keeping the Mac awake, without a second app in the menu bar.
///
/// This is a power assertion and nothing else: no `caffeinate` subprocess to
/// supervise, no timer burning a wakeup every second, no state to lose. macOS
/// holds the assertion, macOS drops it when this process dies, and `pmset -g
/// assertions` shows it under a name a person can read -- which matters here
/// more than in most apps, because procwatch's own sleep report reads that
/// list. Turning this on and then being told "something is holding your Mac
/// awake" is correct, and the name is what makes it useful rather than a
/// mystery.
///
/// The assertion prevents *display* idle sleep, which is the one people mean.
/// Preventing system sleep alone still lets the screen go dark, and every app
/// in this category behaves the way this one does.
final class KeepAwake {
    private var assertion: IOPMAssertionID = IOPMAssertionID(0)
    private var expiry: Timer?
    /// When the current hold ends, or nil for an indefinite hold. Only
    /// meaningful while `isOn`.
    private(set) var endsAt: Date?
    private(set) var isOn = false

    /// Called after any change so the menu bar can follow.
    var onChange: () -> Void = {}

    /// Hold the Mac awake for `seconds`, or indefinitely when nil.
    /// Re-arming while already on replaces the deadline rather than stacking
    /// a second assertion -- two assertions would need two releases, and the
    /// second release would be the one nobody remembers to make.
    func turnOn(seconds: TimeInterval?) {
        release()
        var id = IOPMAssertionID(0)
        let name = seconds == nil
            ? "Procwatch: Keep Awake (until turned off)"
            : "Procwatch: Keep Awake (timed)"
        let result = IOPMAssertionCreateWithName(
            kIOPMAssertionTypeNoDisplaySleep as CFString,
            IOPMAssertionLevel(kIOPMAssertionLevelOn),
            name as CFString,
            &id)
        guard result == kIOReturnSuccess else {
            NSLog("procwatch: could not create the power assertion (\(result))")
            return
        }
        assertion = id
        isOn = true
        if let seconds = seconds {
            endsAt = Date().addingTimeInterval(seconds)
            // Tolerance lets macOS coalesce this with other wakeups. A timer
            // that fires a few seconds late is fine for something measured in
            // minutes, and an exact one is a scheduled wakeup of its own.
            let timer = Timer(timeInterval: seconds, repeats: false) { [weak self] _ in
                self?.turnOff()
            }
            timer.tolerance = 30
            RunLoop.main.add(timer, forMode: .common)
            expiry = timer
        } else {
            endsAt = nil
        }
        onChange()
    }

    func turnOff() {
        release()
        onChange()
    }

    func toggle() {
        isOn ? turnOff() : turnOn(seconds: nil)
    }

    /// Drop the assertion and the deadline, leaving nothing behind. Safe to
    /// call when nothing is held.
    private func release() {
        expiry?.invalidate()
        expiry = nil
        if isOn {
            IOPMAssertionRelease(assertion)
            assertion = IOPMAssertionID(0)
            isOn = false
        }
        endsAt = nil
    }

    /// "on", "1h 4m left", or "off" -- what the menu says about itself.
    var summary: String {
        guard isOn else { return "off" }
        guard let endsAt = endsAt else { return "on" }
        let left = Int(max(0, endsAt.timeIntervalSinceNow))
        let hours = left / 3600, minutes = (left % 3600) / 60
        if hours > 0 { return "\(hours)h \(minutes)m left" }
        return minutes > 0 ? "\(minutes)m left" : "under a minute left"
    }
}

/// Clipboard history, in the menu bar, without a second app for that either.
///
/// NSPasteboard has no change notification, so this polls `changeCount` --
/// an integer compare, not a read of the contents, which is why once a second
/// costs nothing measurable. The contents are only pulled when that integer
/// has actually moved.
///
/// Two limits keep this inside the memory budget the rest of the app is held
/// to: a fixed number of entries, and a cap on how big any one of them may be.
/// Without the second limit a single copied log file becomes a permanent
/// resident, and the history stops being something you can leave running.
final class Clipboard {
    /// Bigger than any sane copied snippet, small enough that even the
    /// largest allowed history is a few megabytes. Anything larger is
    /// remembered as a truncated marker rather than dropped, so the history
    /// does not silently skip a copy.
    static let maxBytes = 32 * 1024

    /// How many entries to keep. Settable, because the right answer differs
    /// by person: a handful for someone who wants the last few things, a few
    /// hundred for someone treating it as a scratch buffer.
    ///
    /// Stored in UserDefaults rather than in the procwatch preferences the
    /// server owns. Those are read over HTTP, and a menu that cannot draw
    /// itself until a socket answers is a menu that sometimes does not draw.
    static let limitKey = "procwatch.clipboard.limit"
    static let defaultLimit = 40
    static let limitChoices = [10, 25, 40, 100, 200]
    /// Below 5 the feature stops being a history; above 500 the menu is
    /// longer than the screen and the file stops being cheap to rewrite on
    /// every copy.
    static let limitRange = 5...500

    var limit: Int {
        get {
            let stored = UserDefaults.standard.integer(forKey: Self.limitKey)
            // integer(forKey:) returns 0 for "never set", which is the one
            // value that must not be taken literally.
            guard stored != 0 else { return Self.defaultLimit }
            return min(max(stored, Self.limitRange.lowerBound),
                       Self.limitRange.upperBound)
        }
        set {
            let clamped = min(max(newValue, Self.limitRange.lowerBound),
                              Self.limitRange.upperBound)
            UserDefaults.standard.set(clamped, forKey: Self.limitKey)
            // Lowering it takes effect now rather than at the next copy --
            // otherwise "keep 10" leaves 200 on disk until something is
            // copied, which is the opposite of what was asked for.
            trim()
            save()
        }
    }

    /// Things worth keeping: an address, a licence key, a command you type
    /// weekly. They sit above the history and never age out of it, which is
    /// the whole point -- a history is a queue, and anything you actually
    /// reuse is exactly the thing a queue eventually throws away.
    static let pinnedKey = "procwatch.clipboard.pinned"
    static let maxPinned = 20

    private(set) var pinned: [String] = []

    private(set) var entries: [String] = []
    private var changeCount = NSPasteboard.general.changeCount
    private var timer: Timer?
    private let store: URL?

    init() {
        let base = FileManager.default.urls(for: .applicationSupportDirectory,
                                            in: .userDomainMask).first
        store = base?.appendingPathComponent("procwatch/clipboard.json")
        load()
    }

    func start() {
        // Once a second is below the rate a person can copy twice, and the
        // work when nothing changed is one integer comparison.
        let timer = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.poll()
        }
        timer.tolerance = 0.3
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    /// Whether a pasteboard is asking not to be recorded.
    ///
    /// Password managers mark what they put on the pasteboard as concealed or
    /// transient, and every clipboard manager is expected to honour it. This
    /// is the difference between a history and a plaintext password log, so it
    /// is checked before the string is read, not after.
    private func isPrivate(_ board: NSPasteboard) -> Bool {
        let names = (board.types ?? []).map { $0.rawValue }
        return names.contains("org.nspasteboard.ConcealedType")
            || names.contains("org.nspasteboard.TransientType")
            || names.contains("org.nspasteboard.AutoGeneratedType")
            || names.contains("com.agilebits.onepassword")
    }

    private func poll() {
        let board = NSPasteboard.general
        guard board.changeCount != changeCount else { return }
        changeCount = board.changeCount
        guard !isPrivate(board) else { return }
        guard let text = board.string(forType: .string) else { return }
        record(text)
    }

    /// Bring `text` to the front of the history, trimming it to the size cap.
    /// Shared by a fresh copy and by restoring an old one, so both obey the
    /// same cap and the same no-duplicates rule.
    private func promote(_ raw: String) {
        let text = Self.capped(raw)
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        // A pinned entry is already kept. Copying it again should not put a
        // second copy in the history below its own pin.
        if pinned.contains(text) { return }
        // A re-copy moves an entry to the top rather than adding a duplicate;
        // copying the same thing twice is one thing you have, not two.
        entries.removeAll { $0 == text }
        entries.insert(text, at: 0)
        trim()
        save()
    }

    private func trim() {
        if entries.count > limit {
            entries.removeLast(entries.count - limit)
        }
    }

    func record(_ raw: String) { promote(raw) }

    /// The size cap, in one place so nothing can enter the history without
    /// passing it -- a live copy, a restore, or a file written by an older
    /// build with a larger cap.
    static func capped(_ text: String) -> String {
        guard text.utf8.count > maxBytes else { return text }
        return String(text.prefix(maxBytes / 4)) + "\u{2026} (truncated)"
    }

    /// Put an entry back on the pasteboard, addressed by its text rather than
    /// its position.
    ///
    /// An index would be read after the menu was built, and the poll timer
    /// runs in .common modes -- so a background app copying something while
    /// the submenu is open shifts every row down, and the click lands on the
    /// entry below the one that was pointed at. Carrying the string closes
    /// that: the row means what it said when it was drawn.
    func restore(_ text: String) {
        let board = NSPasteboard.general
        board.clearContents()
        board.setString(text, forType: .string)
        // Claim the bump this just caused, so poll() does not treat this
        // app's own write as a new copy.
        changeCount = board.changeCount
        promote(text)
    }

    func isPinned(_ text: String) -> Bool {
        return pinned.contains(text)
    }

    /// Pin or unpin, and take it out of the history either way.
    ///
    /// A pinned entry that also sits in the recent list is the same thing
    /// twice in one menu, and the copy you press is then a matter of which
    /// half you looked at first. Pinning promotes it out of the queue;
    /// unpinning hands it back to the top of the queue rather than dropping
    /// it, because unpinning is not deleting.
    func togglePin(_ text: String) {
        if let at = pinned.firstIndex(of: text) {
            pinned.remove(at: at)
            promote(text)
        } else {
            entries.removeAll { $0 == text }
            pinned.insert(Self.capped(text), at: 0)
            if pinned.count > Self.maxPinned {
                pinned.removeLast(pinned.count - Self.maxPinned)
            }
            save()
        }
    }

    func clear() {
        // Only the history. Clearing is for "stop showing me what I have been
        // copying"; the pinned list is a thing you built on purpose and
        // sweeping it away with the same press would be a nasty surprise.
        entries = []
        save()
    }

    func clearPinned() {
        pinned = []
        save()
    }

    private func load() {
        pinned = (UserDefaults.standard.array(forKey: Self.pinnedKey)
                    as? [String] ?? []).prefix(Self.maxPinned).map(Self.capped)
        guard let store = store,
              let data = try? Data(contentsOf: store),
              let saved = try? JSONDecoder().decode([String].self, from: data)
        else { return }
        entries = saved.prefix(limit).map(Self.capped)
    }

    private func save() {
        // Pins live in UserDefaults, not the history file: they are a short
        // list of deliberate choices, and they should survive Clear History
        // and a corrupt or hand-deleted history file alike.
        UserDefaults.standard.set(pinned, forKey: Self.pinnedKey)
        guard let store = store else { return }
        try? FileManager.default.createDirectory(
            at: store.deletingLastPathComponent(),
            withIntermediateDirectories: true)
        guard let data = try? JSONEncoder().encode(entries) else { return }
        // Owner-only: this file is every password manager mistake and every
        // half-written message the user has copied today.
        try? data.write(to: store, options: [.atomic])
        try? FileManager.default.setAttributes([.posixPermissions: 0o600],
                                               ofItemAtPath: store.path)
    }

    /// One line of menu text for an entry: single-line, bounded, and still
    /// recognisable as the thing that was copied.
    static func label(_ text: String) -> String {
        let flat = text.replacingOccurrences(of: "\n", with: " ")
                       .replacingOccurrences(of: "\t", with: " ")
                       .trimmingCharacters(in: .whitespacesAndNewlines)
        let squashed = flat.replacingOccurrences(of: "  +", with: " ",
                                                 options: .regularExpression)
        return squashed.count > 60
            ? String(squashed.prefix(59)) + "\u{2026}"
            : squashed
    }
}

/// A quarter of the screen, near enough: 24% of the visible AREA, kept in
/// the display's own proportions so it reads as a slice of the screen rather
/// than an arbitrary rectangle. sqrt(0.24) is about 0.49, so roughly half the
/// width by half the height -- 705 x 441 on this machine.
/// One size for every page.
///
/// The dashboard used to open at a quarter of the screen and the instruments
/// at most of it, so moving between them resized the window under you --
/// which reads as the panel jumping about rather than as four views of one
/// program. They are all dense pages of charts and lists now, and they all
/// want the same room.
///
/// Still a panel hanging off the menu bar, just a big one: capped in absolute
/// terms so it does not swallow a large display, and proportional below that
/// so it fits a small one.
func panelSize(forMonitor: Bool = true) -> NSSize {
    let visible = NSScreen.main?.visibleFrame.size ?? NSSize(width: 1440, height: 900)
    return NSSize(width: min(1240, visible.width * 0.92).rounded(),
                  height: min(780, visible.height * 0.88).rounded())
}

/// The chart artwork, cropped in, at menu bar height.
///
/// Deliberately not the app icon: most of that tile is padding, so at 18pt the
/// padding is what you see and the chart is a smudge. `procwatch-bar.png` is
/// pre-cropped to the artwork and is wider than it is tall, so the height is
/// pinned to 18pt and the width follows -- the status item uses variable
/// length for exactly this. `isTemplate` stays false: a template image is
/// recoloured to the menu bar's foreground, which would throw the colour away.
///
/// `awake` adds a small dot in the corner while Keep Awake is holding a power
/// assertion. Whether the thing is on is the entire question people have about
/// an app in this category, and answering it in a tooltip means answering it
/// only to someone who already suspected. The dot costs no width.
func menuBarIcon(awake: Bool = false) -> NSImage? {
    let url = Bundle.main.url(forResource: "procwatch-bar", withExtension: "png")
    guard let url = url, let art = NSImage(contentsOf: url) else {
        // No artwork in the bundle (running the binary outside the .app):
        // a glyph is better than an empty menu bar slot.
        let fallback = NSImage(systemSymbolName: awake ? "cup.and.saucer.fill"
                                                       : "chart.line.uptrend.xyaxis",
                               accessibilityDescription: "Procwatch")
        fallback?.isTemplate = true
        return fallback
    }
    let height: CGFloat = 18
    let ratio = art.size.height > 0 ? art.size.width / art.size.height : 1
    let size = NSSize(width: (height * ratio).rounded(), height: height)
    let scaled = NSImage(size: size)
    scaled.lockFocus()
    NSGraphicsContext.current?.imageInterpolation = .high
    art.draw(in: NSRect(origin: .zero, size: size),
             from: .zero, operation: .sourceOver, fraction: 1.0)
    if awake {
        // Bottom-right, inside the artwork, with a hairline of background
        // around it so it reads as a dot on the icon rather than part of the
        // chart. systemOrange is the same accent the unread badge uses.
        let dot: CGFloat = 6
        let rect = NSRect(x: size.width - dot - 1, y: 1, width: dot, height: dot)
        NSColor.windowBackgroundColor.setFill()
        NSBezierPath(ovalIn: rect.insetBy(dx: -1, dy: -1)).fill()
        NSColor.systemOrange.setFill()
        NSBezierPath(ovalIn: rect).fill()
    }
    scaled.unlockFocus()
    scaled.isTemplate = false
    scaled.accessibilityDescription = awake ? "Procwatch, Keep Awake on" : "Procwatch"
    return scaled
}

final class Controller: NSObject, NSApplicationDelegate, NSPopoverDelegate,
                        WKUIDelegate, WKNavigationDelegate,
                        WKScriptMessageHandler,
                        UNUserNotificationCenterDelegate {
    var statusItem: NSStatusItem!
    var popover = NSPopover()
    var webView: WKWebView!
    var port = defaultPort
    var serverProcess: Process?
    var localNetwork: NWBrowser?
    var askedForLocalNetwork = false
    var badgeTimer: Timer?
    var badgeCount = 0
    let keepAwake = KeepAwake()
    let clipboard = Clipboard()
    /// What the last notification said the state was, so re-picking the
    /// setting you are already on does not announce anything.
    var awakeWasOn = false
    // Which of the two pages the panel is showing, so its size follows.
    var showing = "/"
    // What the web view is actually holding, and which server run it was
    // fetched from. Reopening reuses that page rather than fetching it
    // again -- which is what returns you to the scroll position, the open
    // cards and the selected application you left behind. A page from a
    // previous server run is not reused: the token it carries was minted by
    // a process that is gone, so anything it posted would be refused.
    var loadedPath = ""
    var loadedRun = -1
    var serverRun = 0
    /// The page to come back to, remembered across launches.
    var lastPath: String {
        get { UserDefaults.standard.string(forKey: "procwatch.lastPath") ?? "/" }
        set { UserDefaults.standard.set(newValue, forKey: "procwatch.lastPath") }
    }
    // Whether this app may post notifications. Until it may, the queue is
    // left alone -- the recorder falls back to osascript for anything not
    // collected, so declining the permission costs the icon, not the news.
    var canNotify = false

    func applicationDidFinishLaunching(_ note: Notification) {
        if let value = ProcessInfo.processInfo.environment["PROCWATCH_PORT"],
           let parsed = Int(value) {
            port = parsed
        }

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            // The artwork in colour, at menu bar height. Not a template
            // image: templates are recoloured to the menu bar's foreground,
            // which would throw the colour away. The trade-off is that this
            // does not adapt to a light menu bar, which is the point.
            button.image = menuBarIcon()
            button.action = #selector(toggle(_:))
            button.target = self
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }

        // The two things that used to be two other menu bar apps. Keep Awake
        // needs no polling at all; the clipboard needs one integer compare a
        // second. Both are wired to redraw the status item, which is the only
        // place either of them is visible when the panel is shut.
        awakeWasOn = keepAwake.isOn
        keepAwake.onChange = { [weak self] in
            guard let self = self else { return }
            self.showBadge(self.badgeCount)
            self.announceKeepAwake()
        }
        clipboard.start()

        // Notifications as this app, not as Script Editor. Posting through
        // osascript credits the scripting host, because a bare binary is
        // nothing macOS can attribute a notification to; this bundle is. The
        // recorder queues what it wants said, the badge poll below collects
        // it, and a press on the notification opens the panel it is about.
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            self.canNotify = granted
        }

        // How many findings have not been looked at, in the menu bar itself.
        // A minute is often enough for something that is checked by glancing at
        // it, and rare enough that the recorder is not answering a request every
        // few seconds for a number that changes a handful of times a day.
        refreshBadge()
        badgeTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { _ in
            self.refreshBadge()
        }

        let config = WKWebViewConfiguration()
        // The page tells us when a Mac is being added, so the prompt appears
        // beside the action that needs it rather than out of nowhere.
        config.userContentController.add(self, name: "procwatch")
        webView = WKWebView(frame: NSRect(origin: .zero, size: panelSize()),
                            configuration: config)
        webView.setValue(false, forKey: "drawsBackground")
        webView.uiDelegate = self
        webView.navigationDelegate = self

        let hosting = NSViewController()
        hosting.view = webView
        popover.contentViewController = hosting
        popover.contentSize = panelSize()
        popover.behavior = .transient
        popover.animates = false
        popover.delegate = self

        ensureServer()
        // If Macs are already set up, the reason exists before anything is
        // clicked -- someone who restarts should not have to open the panel
        // to make their devices work again. Delayed so the server it asks is
        // listening; still silent on an install with no devices.
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
            self.askForLocalNetworkIfNeeded()
        }
    }

    /// Ask only once something needs it.
    ///
    /// Ask the recorder how many findings are waiting, and show the count.
    ///
    /// The icon is replaced by the count rather than sitting beside it: the menu
    /// bar is the most contended strip on the screen, and a permanent badge on a
    /// logo is the kind of thing people remove the app to get rid of. When there
    /// is nothing to report it is the logo and nothing else.
    func refreshBadge() {
        // notes=1 collects the notification queue, and collecting is
        // claiming -- so it is only sent when this app may actually post
        // them. Otherwise the recorder's osascript fallback delivers.
        let path = canNotify ? "/api/badge?notes=1" : "/api/badge"
        guard let url = URL(string: "http://127.0.0.1:\(port)\(path)") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 3
        URLSession.shared.dataTask(with: request) { data, _, _ in
            guard let data = data,
                  let payload = try? JSONSerialization.jsonObject(with: data)
                      as? [String: Any] else { return }
            let enabled = (payload["enabled"] as? Bool) ?? true
            let count = enabled ? ((payload["count"] as? Int) ?? 0) : 0
            DispatchQueue.main.async { self.showBadge(count) }
            for note in (payload["notes"] as? [[String: Any]]) ?? [] {
                self.postNote(title: (note["title"] as? String) ?? "Procwatch",
                              body: (note["body"] as? String) ?? "",
                              target: (note["target"] as? String) ?? "")
            }
        }.resume()
    }

    /// One notification, attributed to this bundle, carrying where a press
    /// should land.
    func postNote(title: String, body: String, target: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.userInfo = ["target": target]
        let request = UNNotificationRequest(identifier: UUID().uuidString,
                                            content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    /// Show banners even though an accessory app counts as "frontmost" to
    /// the notification centre; without this, its own notifications are
    /// silently swallowed.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler:
                                    @escaping (UNNotificationPresentationOptions) -> Void) {
        if #available(macOS 11.0, *) {
            completionHandler([.banner])
        } else {
            completionHandler([.alert])
        }
    }

    /// A press on a notification opens the panel on the place the news is
    /// about -- the verdict for a finding, the process for an alert, the
    /// timeline for an update. The page reads the fragment and navigates.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let target = (response.notification.request.content
                        .userInfo["target"] as? String) ?? ""
        DispatchQueue.main.async {
            self.openPanel(at: target)
            completionHandler()
        }
    }

    /// The panel, opened on a specific part of the dashboard.
    /// A press on a notification is a destination rather than a return, so
    /// this one does load the page: it has been told where to go.
    func openPanel(at target: String) {
        guard let button = statusItem.button else { return }
        ensureServer()
        if badgeCount > 0 { showBadge(0) }
        showing = "/"
        lastPath = "/"
        popover.contentSize = panelSize()
        let fragment = target.isEmpty ? "" :
            "#" + (target.addingPercentEncoding(
                withAllowedCharacters: .urlFragmentAllowed) ?? "")
        var request = URLRequest(url: URL(string:
            "http://127.0.0.1:\(port)/\(fragment)")!)
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        webView.load(request)
        if !popover.isShown {
            popover.show(relativeTo: button.bounds, of: button,
                         preferredEdge: .minY)
        }
        popover.contentViewController?.view.window?.makeKey()
        NSApp.activate(ignoringOtherApps: true)
    }

    func showBadge(_ count: Int) {
        badgeCount = count
        guard let button = statusItem.button else { return }
        if count <= 0 {
            button.image = menuBarIcon(awake: keepAwake.isOn)
            button.title = ""
            button.toolTip = keepAwake.isOn
                ? "Procwatch \u{2014} Keep Awake \(keepAwake.summary)"
                : "Procwatch"
            return
        }
        // The image goes while the count is up. Leaving both would make the
        // status item twice as wide for as long as anything is unread.
        button.image = nil
        let text = "\u{25C9} \(count)"
        let colour = NSColor.systemOrange
        button.attributedTitle = NSAttributedString(
            string: text,
            attributes: [.foregroundColor: colour,
                         .font: NSFont.systemFont(ofSize: 12,
                                                  weight: .semibold)])
        button.toolTip = count == 1
            ? "Procwatch: 1 finding you have not read"
            : "Procwatch: \(count) findings you have not read"
    }

    /// A process monitor requesting access to your network before you have
    /// asked it to look at anything reads as the tool doing something it has
    /// not explained. So the question is raised when there is a reason for it:
    /// when a Mac has been added, or at the moment one is being added.
    func askForLocalNetworkIfNeeded() {
        if askedForLocalNetwork { return }
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/peers") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2
        URLSession.shared.dataTask(with: request) { data, _, _ in
            guard let data = data,
                  let list = try? JSONSerialization.jsonObject(with: data) as? [Any],
                  !list.isEmpty else { return }
            DispatchQueue.main.async { self.askForLocalNetwork() }
        }.resume()
    }

    /// Raise the macOS local network prompt, from the app itself.
    ///
    /// The dashboard reaches other Macs through a Python process this app
    /// starts. macOS refuses that until the app has been allowed, and reports
    /// the refusal as "no route to host" -- which reads as a network fault and
    /// is not one. Worse, the app never appeared in System Settings at all,
    /// because nothing had ever asked on its behalf.
    ///
    /// Browsing for a Bonjour service is the documented way to raise it.
    /// Nothing is expected to answer; the request is the point.
    func askForLocalNetwork() {
        if askedForLocalNetwork { return }
        askedForLocalNetwork = true
        let browser = NWBrowser(for: .bonjour(type: "_procwatch._tcp", domain: nil),
                                using: NWParameters())
        browser.stateUpdateHandler = { _ in }
        browser.start(queue: .main)
        localNetwork = browser
        // Long enough for the prompt to be raised, then stopped: this exists
        // to ask the question, not to discover anything.
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) {
            browser.cancel()
            self.localNetwork = nil
        }
    }

    /// Start the server only if nothing is already serving.
    ///
    /// Checked by connecting rather than by looking for a process, because the
    /// user may well have started one from a terminal and a second server on
    /// the same port would simply fail to bind.
    ///
    /// The whole tool is one Python file carried inside this bundle, so the app
    /// works wherever it is dragged. It previously ran out of a hardcoded
    /// checkout under ~/Developer, which meant it only worked on the machine it
    /// was written on.
    func ensureServer() {
        if isServing() { return }
        guard let script = Bundle.main.url(forResource: "procwatch",
                                           withExtension: "py") else {
            NSLog("procwatch: procwatch.py is missing from the app bundle")
            return
        }
        for interpreter in Self.interpreters() {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: interpreter)
            task.arguments = [script.path, "serve", "--port", String(port)]
            task.standardOutput = FileHandle.nullDevice
            task.standardError = FileHandle.nullDevice
            do {
                try task.run()
                serverProcess = task
                serverRun += 1
                return
            } catch {
                continue
            }
        }
        NSLog("procwatch: no usable python3 was found")
    }

    /// Which python3 to run the server with, best first.
    ///
    /// /usr/bin/python3 comes first because of Full Disk Access, not because of
    /// its version. macOS lets a child process inherit this app's permissions
    /// only when the child is an Apple-signed platform binary; a Homebrew
    /// python is ad-hoc signed, so it is judged on its own and holds no grant.
    /// Running through `env` picked the Homebrew one, and the effect was that
    /// granting Procwatch Full Disk Access changed nothing at all -- the scan
    /// went on quietly skipping Mail, Messages, Safari and the Photos library
    /// while System Settings showed the switch turned on.
    ///
    /// The fallbacks are for a Mac without the command line tools, where
    /// /usr/bin/python3 is a shim that cannot run anything. There the scan
    /// works and those few folders stay unreadable, which the dashboard says.
    static func interpreters() -> [String] {
        var found: [String] = []
        // /usr/bin/python3 is a shim. Without the command line tools behind it
        // it runs nothing and offers to install them, so it is only worth
        // preferring when something is actually there -- tested by looking,
        // because asking it would raise the very dialog being avoided.
        let backings = ["/Library/Developer/CommandLineTools/usr/bin/python3",
                        "/Applications/Xcode.app/Contents/Developer/usr/bin/python3"]
        if backings.contains(where: FileManager.default.isExecutableFile(atPath:)) {
            found.append("/usr/bin/python3")
        }
        for path in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
        where FileManager.default.isExecutableFile(atPath: path) {
            found.append(path)
        }
        return found.isEmpty ? ["/usr/bin/python3"] : found
    }

    func isServing() -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/info") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 0.6
        let semaphore = DispatchSemaphore(value: 0)
        var alive = false
        URLSession.shared.dataTask(with: request) { _, response, _ in
            alive = (response as? HTTPURLResponse)?.statusCode == 200
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + 1.0)
        return alive
    }

    @objc func toggle(_ sender: NSStatusBarButton) {
        if NSApp.currentEvent?.type == .rightMouseUp {
            return showMenu(sender)
        }
        if popover.isShown {
            popover.performClose(sender)
            return
        }
        // Opening the panel is reading them. The count clears now rather than
        // when the page happens to finish loading, so pressing the badge does
        // what pressing a badge is expected to do; the page marks them read on
        // the recorder's side, and the next poll agrees.
        if badgeCount > 0 { showBadge(0) }
        // Back to whichever page was last open, still scrolled where it was.
        present(lastPath)
        askForLocalNetworkIfNeeded()
    }

    /// A link asking for a new window opens in the real browser.
    ///
    /// WKWebView does nothing at all with target="_blank" unless this is
    /// implemented -- the click is simply swallowed -- so the dashboard's
    /// "Open in browser" button would look broken inside the panel.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            // The dashboard's link to the network monitor opens the window
            // this app already knows how to make, rather than bouncing the
            // user out to a browser for a page that belongs here.
            // The dashboard's links to the instruments open the panel this app
            // already knows how to make, rather than bouncing the user out to
            // a browser for a page that belongs here.
            if url.path == "/net" {
                openMonitor()
                return nil
            }
            if url.path == "/disk" {
                openStorage()
                return nil
            }
            if url.path == "/battery" {
                openBattery()
                return nil
            }
            NSWorkspace.shared.open(url)
            popover.performClose(nil)
        }
        return nil   // nil means "no new web view"; the browser has it now
    }

    /// Run something modal without the panel vanishing underneath it.
    ///
    /// A transient popover closes as soon as it stops being key, and a modal
    /// alert takes key. Without this the dialog appears and the panel behind
    /// it disappears, so answering the question returns you to nothing.
    func runHoldingPanel(_ body: () -> NSApplication.ModalResponse)
            -> NSApplication.ModalResponse {
        let previous = popover.behavior
        popover.behavior = .applicationDefined
        defer { popover.behavior = previous }
        return body()
    }

    /// JavaScript dialogs.
    ///
    /// WKWebView answers confirm() with false and alert() with nothing unless
    /// these are implemented -- silently, with no error anywhere. The Quit and
    /// Force quit buttons ask for confirmation before signalling anything, so
    /// in the panel they appeared to do nothing at all while working perfectly
    /// in a browser.
    func webView(_ webView: WKWebView,
                 runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        completionHandler(runHoldingPanel { alert.runModal() } == .alertFirstButtonReturn)
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        _ = runHoldingPanel { alert.runModal() }
        completionHandler()
    }

    func userContentController(_ controller: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        if (message.body as? String) == "localNetwork" {
            askForLocalNetwork()
        }
    }

    /// The network monitor, in the panel it was asked for from.
    ///
    /// Not a window and not a browser: it opens where the icon is, like
    /// everything else here. The panel grows while the monitor is up --
    /// three panes and a globe do not fit in a quarter of the screen -- and
    /// shrinks back when the dashboard returns, which navigationDelegate
    /// below does by watching where the page went.
    @objc func openMonitor() {
        present("/net")
    }

    /// Where the disk went, in the panel it was asked for from.
    ///
    /// This used to be a sheet thrown over the dashboard: a full-screen
    /// question asked in a quarter-screen panel, with a page refreshing itself
    /// every two seconds behind it. It is an instrument, like the monitor, so
    /// it gets what the monitor gets.
    @objc func openStorage() {
        present("/disk")
    }

    /// The battery's condition, and what has been keeping the Mac awake.
    /// Those two questions are one question, which is why the sleep report
    /// lives here rather than on the dashboard it used to sit on.
    @objc func openBattery() {
        present("/battery")
    }

    /// Show one of the two pages, reusing what is already loaded.
    ///
    /// Fetching it again is what sent somebody who was reading the disk
    /// panel back to the top of the page every time they glanced away. The
    /// page is left alone; only its data is told to catch up.
    func present(_ path: String) {
        guard let button = statusItem.button else { return }
        ensureServer()
        showing = path
        lastPath = path
        popover.contentSize = panelSize(forMonitor: isInstrument(path))
        if loadedPath != path || loadedRun != serverRun {
            var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port)\(path)")!)
            // Ignoring the cache: the pages are edited often, and a cached
            // one looks exactly like a broken one.
            request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
            webView.load(request)
        } else {
            webView.evaluateJavaScript(
                "window.procwatchShown && window.procwatchShown()")
        }
        if !popover.isShown {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        }
        popover.contentViewController?.view.window?.makeKey()
    }

    /// Follow the page: whichever of the two it lands on decides how big the
    /// panel should be, so a link, the back link and the menu item all end
    /// up the same size without each having to remember to set it.
    /// The pages that get their own window size. They all do now -- kept as
    /// one list because it is still the place to name a new page, and because
    /// the panel-size decision reads better as a question about the page than
    /// as a constant.
    func isInstrument(_ path: String) -> Bool {
        return path.hasPrefix("/net") || path.hasPrefix("/disk")
            || path.hasPrefix("/battery") || path == "/"
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        let landed = webView.url?.path ?? "/"
        let path = landed.hasPrefix("/net") ? "/net"
                 : (landed.hasPrefix("/disk") ? "/disk"
                 : (landed.hasPrefix("/battery") ? "/battery" : "/"))
        loadedPath = path
        loadedRun = serverRun
        lastPath = path
        if path != showing {
            showing = path
            popover.contentSize = panelSize(forMonitor: isInstrument(path))
        }
    }

    /// A page that failed to load is not a page worth returning to.
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!,
                 withError error: Error) {
        loadedPath = ""
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        loadedPath = ""
    }

    func showMenu(_ sender: NSStatusBarButton) {
        let menu = NSMenu()
        menu.addItem(withTitle: "Network Monitor…", action: #selector(openMonitor),
                     keyEquivalent: "n").target = self
        menu.addItem(withTitle: "Storage…", action: #selector(openStorage),
                     keyEquivalent: "d").target = self
        menu.addItem(withTitle: "Battery…", action: #selector(openBattery),
                     keyEquivalent: "b").target = self
        menu.addItem(.separator())
        menu.addItem(keepAwakeMenu())
        menu.addItem(clipboardMenu())
        menu.addItem(.separator())
        menu.addItem(withTitle: "Open in browser", action: #selector(openBrowser),
                     keyEquivalent: "").target = self
        if #available(macOS 13.0, *) {
            let item = menu.addItem(withTitle: "Open at login",
                                    action: #selector(toggleLaunchAtLogin),
                                    keyEquivalent: "")
            item.target = self
            item.state = launchesAtLogin ? .on : .off
        }
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Procwatch", action: #selector(quit),
                     keyEquivalent: "q").target = self
        statusItem.menu = menu
        sender.performClick(nil)
        statusItem.menu = nil   // restore left-click-opens-panel behaviour
    }

    /// Keep Awake, as a submenu whose title already says the answer.
    ///
    /// The durations are the ones people actually pick: long enough to cover
    /// a build, a call, a talk, or an afternoon. "Until I turn it off" is
    /// last because it is the one that gets left on by accident, and the menu
    /// bar dot is the reminder that it is.
    func keepAwakeMenu() -> NSMenuItem {
        let root = NSMenuItem(title: "Keep Awake: \(keepAwake.summary)",
                              action: nil, keyEquivalent: "")
        let sub = NSMenu()
        let off = sub.addItem(withTitle: "Off", action: #selector(awakeOff),
                              keyEquivalent: "")
        off.target = self
        off.state = keepAwake.isOn ? .off : .on
        sub.addItem(.separator())
        for (label, minutes) in [("For 30 minutes", 30), ("For 1 hour", 60),
                                 ("For 2 hours", 120), ("For 5 hours", 300)] {
            let item = sub.addItem(withTitle: label, action: #selector(awakeTimed(_:)),
                                   keyEquivalent: "")
            item.target = self
            item.tag = minutes
        }
        sub.addItem(.separator())
        let forever = sub.addItem(withTitle: "Until I turn it off",
                                  action: #selector(awakeForever), keyEquivalent: "")
        forever.target = self
        forever.state = (keepAwake.isOn && keepAwake.endsAt == nil) ? .on : .off
        root.submenu = sub
        return root
    }

    /// Say when the hold starts and when it stops.
    ///
    /// The dot on the icon answers "is it on" for anyone already looking at
    /// the menu bar. This is for the two moments the dot is worst at: the
    /// switch itself, which happens while you are looking at a menu that is
    /// closing over the icon, and the timed expiry, which happens when you
    /// are not looking at all. Turning it on and being told nothing is how
    /// a Mac ends up awake all night.
    ///
    /// Only a real change is announced. Picking "1 hour" while already on a
    /// 1-hour hold re-arms the deadline, and that is worth saying; picking
    /// "Off" while already off is not.
    func announceKeepAwake() {
        let isOn = keepAwake.isOn
        let changed = isOn != awakeWasOn
        awakeWasOn = isOn
        guard changed || isOn else { return }
        if isOn {
            let until: String
            if let ends = keepAwake.endsAt {
                let clock = DateFormatter()
                clock.dateFormat = "HH:mm"
                until = "Until \(clock.string(from: ends)) (\(keepAwake.summary))."
            } else {
                until = "Until you turn it off."
            }
            postNote(title: "Keep Awake on",
                     body: "\(until) Your Mac will not sleep on its own.",
                     target: "")
        } else {
            postNote(title: "Keep Awake off",
                     body: "Your Mac can sleep normally again.", target: "")
        }
    }

    @objc func awakeOff() { keepAwake.turnOff() }
    @objc func awakeForever() { keepAwake.turnOn(seconds: nil) }
    @objc func awakeTimed(_ sender: NSMenuItem) {
        keepAwake.turnOn(seconds: TimeInterval(sender.tag * 60))
    }

    /// Clipboard history, newest first, with the entry number as its own
    /// shortcut for the first nine.
    func clipboardMenu() -> NSMenuItem {
        let root = NSMenuItem(title: "Clipboard", action: nil, keyEquivalent: "")
        let sub = NSMenu()
        // Pinned first and unnumbered-by-recency: these are things you chose,
        // so their order should not shuffle every time you copy something.
        if !clipboard.pinned.isEmpty {
            let head = sub.addItem(withTitle: "Pinned", action: nil,
                                   keyEquivalent: "")
            head.isEnabled = false
            for (index, text) in clipboard.pinned.enumerated() {
                addClipboardRow(sub, text: text, pinned: true,
                                shortcut: index < 9 ? "\(index + 1)" : "")
            }
            sub.addItem(.separator())
        }

        if clipboard.entries.isEmpty && clipboard.pinned.isEmpty {
            let empty = sub.addItem(withTitle: "Nothing copied yet",
                                    action: nil, keyEquivalent: "")
            empty.isEnabled = false
        } else if !clipboard.entries.isEmpty {
            if !clipboard.pinned.isEmpty {
                let head = sub.addItem(withTitle: "Recent", action: nil,
                                       keyEquivalent: "")
                head.isEnabled = false
            }
            // The number keys stay with the recent list only when nothing is
            // pinned; otherwise the pins own them, because the pins are what
            // you reach for by muscle memory.
            let numbered = clipboard.pinned.isEmpty
            for (index, text) in clipboard.entries.enumerated() {
                addClipboardRow(sub, text: text, pinned: false,
                                shortcut: (numbered && index < 9)
                                    ? "\(index + 1)" : "")
            }
        }

        sub.addItem(.separator())
        let hint = sub.addItem(withTitle: "Hold ⌥ to pin or unpin",
                               action: nil, keyEquivalent: "")
        hint.isEnabled = false
        if !clipboard.entries.isEmpty {
            sub.addItem(withTitle: "Clear History", action: #selector(clipboardClear),
                        keyEquivalent: "").target = self
        }
        if !clipboard.pinned.isEmpty {
            sub.addItem(withTitle: "Remove All Pins",
                        action: #selector(clipboardClearPins),
                        keyEquivalent: "").target = self
        }
        // Always offered, including when the history is empty -- setting the
        // size before copying anything is a reasonable thing to want to do.
        sub.addItem(.separator())
        sub.addItem(clipboardLimitMenu())
        root.submenu = sub
        return root
    }

    /// How many copies to keep, as a checked list.
    ///
    /// A list rather than a text field: these are the sizes people pick, and
    /// a menu cannot validate typed input without growing a dialog to report
    /// the failure in. The stored value is clamped either way, so a number
    /// set by hand with `defaults write` is still honoured within range.
    func clipboardLimitMenu() -> NSMenuItem {
        let root = NSMenuItem(title: "Keep Last: \(clipboard.limit)",
                              action: nil, keyEquivalent: "")
        let sub = NSMenu()
        let current = clipboard.limit
        for choice in Clipboard.limitChoices {
            let item = sub.addItem(withTitle: "\(choice) copies",
                                   action: #selector(clipboardSetLimit(_:)),
                                   keyEquivalent: "")
            item.target = self
            item.tag = choice
            item.state = choice == current ? .on : .off
        }
        // A value set outside this list still shows, so the menu never
        // reports a size the app is not actually using.
        if !Clipboard.limitChoices.contains(current) {
            sub.addItem(.separator())
            let item = sub.addItem(withTitle: "\(current) copies (set by hand)",
                                   action: nil, keyEquivalent: "")
            item.state = .on
        }
        root.submenu = sub
        return root
    }

    @objc func clipboardSetLimit(_ sender: NSMenuItem) {
        clipboard.limit = sender.tag
    }

    /// One clipboard row, plus its hidden Option-held twin.
    ///
    /// An alternate item is macOS's own idiom for "the same row, the other
    /// verb": it occupies no space, appears the instant Option goes down, and
    /// needs no second menu to hunt through. The alternative was a submenu on
    /// every entry, which turns a one-press copy into a hover-and-aim.
    func addClipboardRow(_ menu: NSMenu, text: String, pinned: Bool,
                         shortcut: String) {
        let label = Clipboard.label(text)
        let item = menu.addItem(withTitle: (pinned ? "📌  " : "") + label,
                                action: #selector(clipboardPick(_:)),
                                keyEquivalent: shortcut)
        item.target = self
        item.representedObject = text
        // The full text on hover: a menu row is 60 characters and the thing
        // you copied often is not.
        item.toolTip = String(text.prefix(2000))

        let alt = menu.addItem(withTitle: (pinned ? "Unpin  " : "Pin  ") + label,
                               action: #selector(clipboardTogglePin(_:)),
                               keyEquivalent: shortcut)
        alt.target = self
        alt.representedObject = text
        alt.keyEquivalentModifierMask = .option
        alt.isAlternate = true
    }

    @objc func clipboardPick(_ sender: NSMenuItem) {
        guard let text = sender.representedObject as? String else { return }
        clipboard.restore(text)
    }

    @objc func clipboardTogglePin(_ sender: NSMenuItem) {
        guard let text = sender.representedObject as? String else { return }
        clipboard.togglePin(text)
    }

    @objc func clipboardClearPins() { clipboard.clearPinned() }

    @objc func clipboardClear() { clipboard.clear() }

    /// Whether macOS launches this at login.
    ///
    /// The recorder already survives a restart -- launchd owns it -- but the
    /// menu bar icon did not, so after every reboot the app was gone until it
    /// was opened by hand. SMAppService registers the bundle itself, which
    /// needs no helper and no login-item shim.
    var launchesAtLogin: Bool {
        if #available(macOS 13.0, *) {
            return SMAppService.mainApp.status == .enabled
        }
        return false
    }

    @objc func toggleLaunchAtLogin() {
        guard #available(macOS 13.0, *) else { return }
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
        } catch {
            NSLog("procwatch: could not change the login item: \(error)")
        }
    }

    @objc func openBrowser() {
        NSWorkspace.shared.open(URL(string: "http://127.0.0.1:\(port)/")!)
    }

    @objc func quit() {
        // The recorder keeps running; only this viewer stops.
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let controller = Controller()
app.delegate = controller
// Accessory rather than regular: no Dock icon, no menu bar of its own.
app.setActivationPolicy(.accessory)
app.run()

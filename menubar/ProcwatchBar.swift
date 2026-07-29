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
import Network
import ServiceManagement
import WebKit

let defaultPort = 8790

/// A quarter of the screen, near enough: 24% of the visible AREA, kept in
/// the display's own proportions so it reads as a slice of the screen rather
/// than an arbitrary rectangle. sqrt(0.24) is about 0.49, so roughly half the
/// width by half the height -- 705 x 441 on this machine.
func panelSize() -> NSSize {
    let visible = NSScreen.main?.visibleFrame.size ?? NSSize(width: 1440, height: 900)
    let scale = (0.24 as CGFloat).squareRoot()
    return NSSize(width: (visible.width * scale).rounded(),
                  height: (visible.height * scale).rounded())
}

/// The chart artwork, cropped in, at menu bar height.
///
/// Deliberately not the app icon: most of that tile is padding, so at 18pt the
/// padding is what you see and the chart is a smudge. `procwatch-bar.png` is
/// pre-cropped to the artwork and is wider than it is tall, so the height is
/// pinned to 18pt and the width follows -- the status item uses variable
/// length for exactly this. `isTemplate` stays false: a template image is
/// recoloured to the menu bar's foreground, which would throw the colour away.
func menuBarIcon() -> NSImage? {
    let url = Bundle.main.url(forResource: "procwatch-bar", withExtension: "png")
    guard let url = url, let art = NSImage(contentsOf: url) else {
        // No artwork in the bundle (running the binary outside the .app):
        // a glyph is better than an empty menu bar slot.
        let fallback = NSImage(systemSymbolName: "chart.line.uptrend.xyaxis",
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
    scaled.unlockFocus()
    scaled.isTemplate = false
    scaled.accessibilityDescription = "Procwatch"
    return scaled
}

final class Controller: NSObject, NSApplicationDelegate, NSPopoverDelegate,
                        WKUIDelegate, WKNavigationDelegate,
                        WKScriptMessageHandler {
    var statusItem: NSStatusItem!
    var popover = NSPopover()
    var webView: WKWebView!
    var port = defaultPort
    var serverProcess: Process?
    var localNetwork: NWBrowser?
    var askedForLocalNetwork = false

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
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        task.arguments = ["python3", script.path, "serve", "--port", String(port)]
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        do {
            try task.run()
            serverProcess = task
        } catch {
            NSLog("procwatch: could not start the server: \(error)")
        }
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
        ensureServer()
        popover.contentSize = panelSize()
        // Reload on every open, ignoring any cached copy: the dashboard is
        // edited often and a cached page looks identical to a broken one.
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/")!)
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        webView.load(request)
        popover.show(relativeTo: sender.bounds, of: sender, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
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

    func showMenu(_ sender: NSStatusBarButton) {
        let menu = NSMenu()
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

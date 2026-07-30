"""What every process on a Mac actually is, and whether to worry about it.

Activity Monitor will tell you that `mds_stores` is using 190% of a core. It
will not tell you that mds_stores is Spotlight, that Spotlight rebuilds its
index after a large amount of new files arrive, that this is expected, that it
finishes on its own, and that the machine will be slower until it does. That
gap -- between a name and an answer -- is what this file closes, and it is the
one part of a process monitor that cannot be derived from measurement. It has
to be known.

Each entry says four things, because those are the four things somebody wants:

  what it is        the human name, not the executable
  does              one sentence, no jargon
  high              what it means when it is busy, which is usually not a fault
  advice            what to do, including "nothing" when that is the truth

Accuracy over coverage. A confident wrong explanation is worse than none, so
anything not listed falls through to a description derived from where the
binary lives and how it behaves -- which is honest about being a deduction.
"""
import os
import re

# Categories, used for grouping and for tone: nobody needs advice about
# WindowServer, and everybody wants it about a runaway helper.
APPLE = "apple"
THIRD = "third-party"
DEV = "developer"
BROWSER = "browser"

# ---------------------------------------------------------------------------
# Spotlight and file indexing
# ---------------------------------------------------------------------------
CATALOGUE = {
    "mds": dict(name="Spotlight", cat=APPLE,
        does="Runs the search index behind Spotlight, Finder search and Mail search.",
        high="Usually rebuilding after a lot of new files arrived, a disk was "
             "attached, or macOS was updated. A full rebuild can take hours.",
        advice="Nothing. It finishes on its own and stops. Leave the Mac "
               "plugged in and it will finish sooner."),
    "mds_stores": dict(name="Spotlight", cat=APPLE,
        does="Writes and compacts the Spotlight index on disk.",
        high="A rebuild in progress. This is the single most common reason a "
             "Mac is mysteriously slow for an hour or two after an update.",
        advice="Nothing. If it never stops for days, the index is likely "
               "corrupt: re-add the disk under Spotlight Privacy to force a "
               "clean rebuild."),
    "mdworker": dict(name="Spotlight", cat=APPLE,
        does="Reads individual files so Spotlight can index their contents.",
        high="Many new or changed files. Several copies run at once by design.",
        advice="Nothing."),
    "mdworker_shared": dict(name="Spotlight", cat=APPLE,
        does="Reads new files to index them, one worker per kind of file.",
        high="Files were just added or changed. Big PDFs and archives are slow "
             "to read.",
        advice="Nothing."),
    "corespotlightd": dict(name="Spotlight", cat=APPLE,
        does="Indexes content apps hand over, such as Mail and Messages.",
        high="An app handed over a large amount of history at once.",
        advice="Nothing."),
    "fseventsd": dict(name="File change log", cat=APPLE,
        does="Records every file change so backups and search know what moved.",
        high="Something is writing a great many files -- a build, an install, a "
             "sync, or an extraction.",
        advice="Find what is writing. The Disk chart names it."),
    "revisiond": dict(name="Document versions", cat=APPLE,
        does="Keeps the version history behind Revert To in Pages and friends.",
        high="A large document was saved repeatedly.",
        advice="Nothing."),

    # -----------------------------------------------------------------------
    # Backup
    # -----------------------------------------------------------------------
    "backupd": dict(name="Time Machine", cat=APPLE,
        does="Runs your backups.",
        high="A backup is running. The first one, or the first after a macOS "
             "update, copies far more and takes far longer.",
        advice="Nothing. Skip this backup from the menu bar if you need the "
               "machine right now."),
    "backupd-helper": dict(name="Time Machine", cat=APPLE,
        does="Decides when a backup should start.",
        high="Briefly, while working out what changed.", advice="Nothing."),
    "mtmd": dict(name="Time Machine snapshots", cat=APPLE,
        does="Takes the local hourly snapshots Time Machine keeps on your disk.",
        high="Taking or thinning a snapshot.",
        advice="Nothing. These are why free space sometimes recovers slowly "
               "after deleting files."),

    # -----------------------------------------------------------------------
    # Photos and media
    # -----------------------------------------------------------------------
    "photoanalysisd": dict(name="Photos", cat=APPLE,
        does="Looks through your photo library for faces, scenes and objects so "
             "search and Memories work.",
        high="Normal after importing photos or a new macOS version. It is "
             "supposed to run only when the Mac is idle and plugged in, and it "
             "is famous for not respecting that.",
        advice="Quit Photos entirely and it pauses. It resumes when idle and "
               "eventually finishes for good."),
    "photolibraryd": dict(name="Photos", cat=APPLE,
        does="Manages the photo library database.",
        high="A large import, or iCloud Photos reconciling.",
        advice="Nothing."),
    "mediaanalysisd": dict(name="Media analysis", cat=APPLE,
        does="Reads images and video for text, subjects and Live Text.",
        high="New media to work through, often after an import or an update.",
        advice="Nothing. It stops when it has caught up."),
    "AMPLibraryAgent": dict(name="Music library", cat=APPLE,
        does="Keeps the Music and TV libraries in order.",
        high="A large library change or an Apple Music sync.",
        advice="Nothing."),
    "coreaudiod": dict(name="Audio", cat=APPLE,
        does="Runs all sound on the Mac.",
        high="Sustained high use is unusual and usually means a misbehaving "
             "audio device, driver or virtual output.",
        advice="Unplug audio interfaces one at a time. Audio plug-ins like "
               "Loopback, Soundflower and BlackHole are common culprits."),

    # -----------------------------------------------------------------------
    # iCloud and sync
    # -----------------------------------------------------------------------
    "bird": dict(name="iCloud Drive", cat=APPLE,
        does="Uploads and downloads iCloud Drive and Desktop & Documents.",
        high="Syncing a large change. Also spins when it cannot finish -- a "
             "file it cannot upload will make it try forever.",
        advice="Check iCloud has space and that no file is stuck. Days of "
               "constant activity means something cannot sync."),
    "cloudd": dict(name="iCloud", cat=APPLE,
        does="Carries CloudKit data for Notes, Reminders, and most apps that "
             "sync.",
        high="An app is syncing a lot, or retrying something it cannot finish.",
        advice="Usually settles. If it never does, sign out and back in to "
               "iCloud."),
    "syncdefaultsd": dict(name="iCloud settings sync", cat=APPLE,
        does="Syncs small preferences between your devices.",
        high="Unusual for long. Often an app writing a setting in a loop.",
        advice="Nothing immediately; note which app you were using."),
    "nsurlsessiond": dict(name="Background downloads", cat=APPLE,
        does="Does downloading on behalf of other apps -- App Store, iCloud, "
             "podcasts, software updates.",
        high="Something large is downloading. It has no window, so this is "
             "often the answer to 'why is my network busy'.",
        advice="Check the Network chart and the App Store."),
    "apsd": dict(name="Push notifications", cat=APPLE,
        does="Keeps the connection that delivers push notifications.",
        high="Rare. Usually a network problem causing reconnection loops.",
        advice="Nothing. Check your network if it persists."),
    "sharingd": dict(name="Continuity", cat=APPLE,
        does="Runs AirDrop, Handoff, Universal Clipboard and Sidecar.",
        high="An AirDrop transfer, or repeatedly looking for nearby devices.",
        advice="Turning Bluetooth off stops it, at the cost of Continuity."),
    "rapportd": dict(name="Continuity", cat=APPLE,
        does="Lets your Mac talk to your iPhone and iPad for Handoff and "
             "phone calls.",
        high="Looking for nearby devices, often when Wi-Fi is flaky.",
        advice="Nothing."),
    "mDNSResponder": dict(name="Network discovery", cat=APPLE,
        does="Finds printers, AirPlay devices and shared drives on your "
             "network, and resolves every name your Mac looks up.",
        high="A busy or noisy network, or an app resolving names in a loop.",
        advice="Nothing usually. Sustained high use with slow browsing points "
               "at a DNS problem."),

    # -----------------------------------------------------------------------
    # Security
    # -----------------------------------------------------------------------
    "XprotectService": dict(name="XProtect", cat=APPLE,
        does="Scans for known malware. Part of macOS, not an add-on.",
        high="Scanning something you just downloaded or launched.",
        advice="Nothing. It is doing the job."),
    "XProtect": dict(name="XProtect", cat=APPLE,
        does="Checks files against Apple's malware list.",
        high="A first launch or a new download.", advice="Nothing."),
    "syspolicyd": dict(name="Gatekeeper", cat=APPLE,
        does="Decides whether an app is allowed to run, and checks its "
             "notarisation with Apple.",
        high="The first launch of a new app -- this is why a newly installed "
             "app can take twenty seconds to open once and be instant after.",
        advice="Nothing."),
    "trustd": dict(name="Certificate checks", cat=APPLE,
        does="Verifies the certificates behind every secure connection.",
        high="Many new connections, or a certificate check that keeps failing.",
        advice="Nothing. Persistent high use can mean a network appliance "
               "intercepting traffic."),
    "tccd": dict(name="Privacy permissions", cat=APPLE,
        does="Enforces which apps may reach your files, camera, microphone and "
             "screen.",
        high="An app repeatedly asking for something it has been denied.",
        advice="Look in Privacy & Security for an app you refused."),
    "secd": dict(name="Keychain sync", cat=APPLE,
        does="Syncs passwords through iCloud Keychain.",
        high="Unusual for long; often a sync it cannot complete.",
        advice="Nothing."),
    "endpointsecurityd": dict(name="Security extensions", cat=APPLE,
        does="Hosts security tools that watch what programs do.",
        high="Almost always caused by third-party security software attached "
             "to it, not by macOS.",
        advice="If you have antivirus or endpoint software installed, that is "
               "the actual source."),

    # -----------------------------------------------------------------------
    # Interface
    # -----------------------------------------------------------------------
    "WindowServer": dict(name="The display system", cat=APPLE,
        does="Draws everything you see. Every window, every animation, every "
             "pixel goes through it.",
        high="Many windows, several displays, a high refresh rate, or an app "
             "redrawing constantly. Scales with what is on screen, so it is "
             "high while you work and that is normal.",
        advice="If it is high with little on screen, an app is animating "
               "something invisible. Close windows and watch which one drops "
               "it. External displays and screen recording raise it a lot."),
    "Dock": dict(name="The Dock", cat=APPLE,
        does="Runs the Dock, Mission Control and Stage Manager.",
        high="Unusual. Sometimes a badge or animation stuck in a loop.",
        advice="killall Dock restarts it harmlessly."),
    "Finder": dict(name="Finder", cat=APPLE,
        does="The desktop and every file window.",
        high="A folder with very many files, calculating folder sizes, or a "
             "slow network share.",
        advice="Turn off Calculate All Sizes in View Options. A hung network "
               "share is a common cause."),
    "distnoted": dict(name="Notification relay", cat=APPLE,
        does="Passes small messages between programs.",
        high="An app broadcasting messages in a tight loop.",
        advice="It is a symptom, not the cause. Whatever app is busy alongside "
               "it is the one to look at."),
    "cfprefsd": dict(name="Preferences", cat=APPLE,
        does="Reads and writes app settings.",
        high="An app writing a preference over and over, which is always a bug "
             "in that app.",
        advice="A genuinely useful clue: whichever app is busy at the same time "
               "is misbehaving."),
    "launchservicesd": dict(name="App registry", cat=APPLE,
        does="Keeps track of which app opens which kind of file.",
        high="Rebuilding after installing or moving applications.",
        advice="Nothing."),
    "iconservicesagent": dict(name="Icons", cat=APPLE,
        does="Builds and caches icons.",
        high="Rebuilding the icon cache, usually after an update or a large "
             "install.",
        advice="Nothing."),
    "fontd": dict(name="Fonts", cat=APPLE,
        does="Manages installed fonts.",
        high="Validating a newly installed font collection. Broken or "
             "duplicate fonts can make it spin.",
        advice="Font Book's Validate can find a bad font."),

    # -----------------------------------------------------------------------
    # Siri, suggestions, on-device intelligence
    # -----------------------------------------------------------------------
    "corespeechd": dict(name="Speech", cat=APPLE,
        does="Listens for 'Hey Siri' and handles dictation.",
        high="Dictation in use, or the wake phrase listener restarting.",
        advice="Turning off Hey Siri or Dictation stops it."),
    "assistantd": dict(name="Siri", cat=APPLE,
        does="Runs Siri itself.",
        high="A request in progress, or Siri retrying.", advice="Nothing."),
    "siriknowledged": dict(name="Siri knowledge", cat=APPLE,
        does="Keeps the on-device data Siri and Spotlight suggestions use.",
        high="Rebuilding after an update.", advice="Nothing."),
    "suggestd": dict(name="Suggestions", cat=APPLE,
        does="Finds names, dates and places in your mail and messages so they "
             "can be offered as suggestions.",
        high="Working through a large mailbox, often after adding an account.",
        advice="Turning off Siri Suggestions for Mail stops it."),
    "parsecd": dict(name="Spotlight suggestions", cat=APPLE,
        does="Fetches the web and App Store results Spotlight shows.",
        high="Every Spotlight search reaches the network through this.",
        advice="Turn off Siri Suggestions in Spotlight settings if unwanted."),
    "knowledge-agent": dict(name="On-device activity store", cat=APPLE,
        does="Records small facts about what you do so Siri and Screen Time "
             "can use them.",
        high="Unusual. Sometimes a database that needs rebuilding.",
        advice="Nothing."),
    "proactived": dict(name="Proactive suggestions", cat=APPLE,
        does="Predicts what you might want next.",
        high="Unusual for long.", advice="Nothing."),
    "duetexpertd": dict(name="Suggestions engine", cat=APPLE,
        does="Decides which suggestions to show and when.",
        high="Unusual. A known occasional runaway.",
        advice="Nothing; it settles after a restart."),
    "biomesyncd": dict(name="Activity sync", cat=APPLE,
        does="Syncs the small activity records behind Siri and Screen Time "
             "between your devices.",
        high="Catching up after being offline.", advice="Nothing."),
    "triald": dict(name="Feature trials", cat=APPLE,
        does="Runs Apple's on-device experiments.",
        high="Unusual and harmless.", advice="Nothing."),
    "ScreenTimeAgent": dict(name="Screen Time", cat=APPLE,
        does="Records app usage for Screen Time.",
        high="Reconciling usage across devices.", advice="Nothing."),
    "spotlightknowledged": dict(name="Spotlight knowledge", cat=APPLE,
        does="Builds the on-device store of people, places and topics that "
             "Spotlight and Siri search across.",
        high="Working through new mail, messages or files, usually overnight "
             "or after an update.",
        advice="Nothing. It is one of the most common names on macOS's own "
               "excess-CPU reports and finishes on its own."),
    "siriactionsd": dict(name="Shortcuts and Siri actions", cat=APPLE,
        does="Runs the actions Shortcuts and Siri can perform, and keeps the "
             "list of what each app offers.",
        high="Re-reading what your apps can do, which it does after installs "
             "and updates. A frequent offender on excess-CPU reports.",
        advice="Nothing usually. If it is constant, a recently installed app "
               "is publishing a broken shortcut."),
    "hybridsearchd": dict(name="Search index", cat=APPLE,
        does="Builds the newer on-device search index that ranks results "
             "across Mail, Messages and files.",
        high="Indexing, most often after an update.",
        advice="Nothing."),
    "dasd": dict(name="Background task scheduler", cat=APPLE,
        does="Decides when background jobs are allowed to run -- it is what "
             "waits for your Mac to be idle, cool and charged.",
        high="Unusual. It is a scheduler, so being busy means it is thrashing "
             "over what to run rather than doing the work itself.",
        advice="Nothing. Whatever it launched is the thing to look at."),
    "PerfPowerServices": dict(name="Battery diagnostics", cat=APPLE,
        does="Collects the power and performance data behind battery health "
             "and Screen Time's energy figures.",
        high="Processing a backlog, often right after a wake.",
        advice="Nothing, and the irony of the battery reporter using battery "
               "is not lost on anybody."),
    "ANECompilerService": dict(name="Neural engine compiler", cat=APPLE,
        does="Compiles machine-learning models so they can run on the Apple "
             "Neural Engine -- the chip that does face recognition, Live Text "
             "and dictation.",
        high="An app asked for a model to be prepared. It is heavy, brief, and "
             "cached afterwards, so the same model is not compiled twice.",
        advice="Nothing. Expect it after installing an app that does anything "
               "with images or speech."),
    "fontworker": dict(name="Fonts", cat=APPLE,
        does="Reads font files on behalf of the font server.",
        high="A newly installed font collection being validated.",
        advice="Nothing. A broken or duplicated font can make it spin -- Font "
               "Book's Validate finds it."),
    "logd": dict(name="The system log", cat=APPLE,
        does="Collects everything every program writes to the unified log.",
        high="Something is logging enormously. logd is the symptom; the "
             "program filling the log is the cause.",
        advice="A useful clue rather than a problem: whatever else is busy "
               "alongside it is writing far too much."),
    "MenuBarAgent": dict(name="A menu bar item", cat=THIRD,
        does="The menu bar part of an application, kept separate so it can run "
             "while the app itself is closed.",
        high="Whatever the app behind it is doing.",
        advice="Quit it from its own menu."),
    "Mail": dict(name="Mail", cat=APPLE,
        does="Apple's mail app.",
        high="Fetching a large mailbox, rebuilding its index, or stuck on one "
             "message it cannot download.",
        advice="Mailbox > Rebuild on the affected mailbox fixes the stuck "
               "cases."),
    "WhatsApp": dict(name="WhatsApp", cat=THIRD,
        does="The WhatsApp desktop app, which is a packaged web app.",
        high="Syncing history, or one conversation with a lot of media.",
        advice="Quitting and reopening it clears most of it."),
    "AppleSpell": dict(name="Spelling", cat=APPLE,
        does="Checks spelling for every app.",
        high="A large document, or a learned-words file that has grown broken.",
        advice="Nothing usually."),

    # -----------------------------------------------------------------------
    # Kernel, power, hardware
    # -----------------------------------------------------------------------
    "kernel_task": dict(name="macOS itself", cat=APPLE,
        does="Not really a process. When it shows high CPU, macOS is "
             "deliberately occupying cores to keep them from being used -- "
             "which it does to cool the machine down.",
        high="Your Mac is hot and is being throttled. The number is the "
             "symptom of heat, not a program misbehaving.",
        advice="Check what was busy just before. Move the Mac off soft "
               "furnishings, and check the charger -- a failing or "
               "underpowered one causes this. It is protective, not a fault."),
    "powerd": dict(name="Power management", cat=APPLE,
        does="Decides when the Mac sleeps and manages the battery.",
        high="Very unusual.", advice="Nothing."),
    "hidd": dict(name="Input devices", cat=APPLE,
        does="Handles the keyboard, trackpad, mouse and every other input "
             "device.",
        high="A faulty or noisy input device, often a mouse or a tablet "
             "sending a flood of events.",
        advice="Unplug input devices one at a time."),
    "opendirectoryd": dict(name="Accounts and directory", cat=APPLE,
        does="Looks up users and groups, including corporate directories.",
        high="On a managed Mac, a directory server that is slow or "
             "unreachable.",
        advice="On a work Mac, mention it to IT. On a personal one it is "
               "usually brief."),
    "diskarbitrationd": dict(name="Disk mounting", cat=APPLE,
        does="Mounts and unmounts disks and images.",
        high="A disk that keeps appearing and disappearing, or a failing "
             "drive.",
        advice="Sustained activity suggests a drive or cable problem."),
    "KernelEventAgent": dict(name="Disk warnings", cat=APPLE,
        does="Raises the 'disk is almost full' and 'server not responding' "
             "warnings.",
        high="A full disk or an unresponsive network share.",
        advice="Check free space, and unmount any share that has gone away."),

    # -----------------------------------------------------------------------
    # Updates, installs, diagnostics
    # -----------------------------------------------------------------------
    "softwareupdated": dict(name="Software Update", cat=APPLE,
        does="Finds, downloads and prepares macOS updates.",
        high="An update is downloading or being prepared, often before it has "
             "told you.",
        advice="Nothing. This is a common answer to 'why was it slow "
               "overnight'."),
    "installd": dict(name="Installer", cat=APPLE,
        does="Installs packages.",
        high="Something is installing.", advice="Nothing."),
    "storedownloadd": dict(name="App Store", cat=APPLE,
        does="Downloads App Store purchases and updates.",
        high="A download in progress.", advice="Nothing."),
    "ReportCrash": dict(name="Crash reporter", cat=APPLE,
        does="Writes a report when a program crashes.",
        high="Repeated appearances mean something is crashing over and over, "
             "which is worth knowing even if you never saw a window.",
        advice="Whatever crashed is in Console under Crash Reports. Repeated "
               "crashes of a background helper are a real fault."),
    "spindump": dict(name="Hang reporter", cat=APPLE,
        does="Records what an unresponsive program was doing.",
        high="Something has hung. macOS is capturing evidence about it.",
        advice="A strong signal: something on the machine stopped responding "
               "at that moment."),
    "analyticsd": dict(name="Diagnostics", cat=APPLE,
        does="Collects the diagnostic data macOS may send to Apple.",
        high="Processing a backlog.",
        advice="Turning off analytics sharing in Privacy & Security stops it."),
    "osanalyticshelper": dict(name="Diagnostics", cat=APPLE,
        does="Tidies up and submits diagnostic reports.",
        high="Often follows a crash or a hang.",
        advice="Nothing, but look for what crashed."),

    # -----------------------------------------------------------------------
    # Developer tooling
    # -----------------------------------------------------------------------
    "swift-frontend": dict(name="Swift compiler", cat=DEV,
        does="Compiles Swift. One process per file, so many at once.",
        high="A build is running. Expected to use every core.",
        advice="Nothing. This is the machine doing what you asked."),
    "clang": dict(name="C/C++ compiler", cat=DEV,
        does="Compiles C, C++ and Objective-C.",
        high="A build.", advice="Nothing."),
    "SourceKitService": dict(name="Xcode code completion", cat=DEV,
        does="Provides Xcode's autocomplete and syntax checking.",
        high="Known to run away on large Swift files and stay there.",
        advice="Safe to force quit -- Xcode restarts it and completion comes "
               "back."),
    "XCBBuildService": dict(name="Xcode build system", cat=DEV,
        does="Plans and runs Xcode builds.",
        high="A build.", advice="Nothing."),
    "com.docker.backend": dict(name="Docker", cat=THIRD,
        does="Runs the Linux virtual machine your containers live in.",
        high="Containers doing work, or a container in a crash loop. Docker's "
             "CPU is charged to this one process, so it hides which container "
             "is responsible.",
        advice="docker stats names the actual container."),
    "qemu-system-aarch64": dict(name="A virtual machine", cat=THIRD,
        does="Runs a virtual machine.",
        high="The guest is busy. The Mac cannot see inside it.",
        advice="Look inside the virtual machine."),
    "node": dict(name="Node.js", cat=DEV,
        does="Runs JavaScript outside a browser -- dev servers, build tools, "
             "and a lot of desktop app plumbing.",
        high="A build, a watcher, or a script that will not end.",
        advice="The command line in the details says which script."),
    "python3": dict(name="Python", cat=DEV,
        does="Runs Python programs, including many system and developer tools.",
        high="Whatever script it is running.",
        advice="The command line names the script."),
    "ruby": dict(name="Ruby", cat=DEV,
        does="Runs Ruby programs.", high="Whatever script it is running.",
        advice="The command line names the script."),

    # -----------------------------------------------------------------------
    # Third-party that people meet
    # -----------------------------------------------------------------------
    "Google Chrome Helper": dict(name="A Chrome tab or extension", cat=BROWSER,
        does="One of these exists per tab, per extension and per plug-in.",
        high="A single tab or extension. Chrome's own Task Manager (Window "
             "menu) names it exactly.",
        advice="Use Chrome's Task Manager rather than guessing."),
    "com.apple.WebKit.WebContent": dict(name="A Safari tab", cat=BROWSER,
        does="Renders one Safari tab or web view.",
        high="One heavy page, often something with video or a lot of "
             "JavaScript.",
        advice="Safari's Develop menu, or close tabs and watch which one drops "
               "it."),
    "Dropbox": dict(name="Dropbox", cat=THIRD,
        does="Syncs your Dropbox folder.",
        high="Indexing or a large sync. Very high use for hours usually means "
             "it is stuck on one file.",
        advice="Pause syncing to confirm it is the cause."),
    "OneDrive": dict(name="OneDrive", cat=THIRD,
        does="Syncs OneDrive.", high="A large sync, or a stuck file.",
        advice="Pause syncing to confirm."),
    "Google Drive": dict(name="Google Drive", cat=THIRD,
        does="Syncs Google Drive and provides its virtual disk.",
        high="Syncing, or scanning the mounted volume.",
        advice="Pause syncing to confirm."),
    "Microsoft AutoUpdate": dict(name="Microsoft AutoUpdate", cat=THIRD,
        does="Checks for updates to Office.",
        high="A check or download. Runs whether or not Office is open.",
        advice="Set it to check monthly rather than daily."),
    "AdobeIPCBroker": dict(name="Adobe background service", cat=THIRD,
        does="Connects Adobe apps to Creative Cloud.",
        high="Common even with no Adobe app open.",
        advice="Creative Cloud's settings can stop it launching at login."),
    "CCXProcess": dict(name="Adobe Creative Cloud", cat=THIRD,
        does="Runs part of the Creative Cloud desktop app.",
        high="Well known for using CPU while doing nothing visible.",
        advice="Safe to quit; Creative Cloud starts it again when needed."),
    "bzfilelist": dict(name="Backblaze", cat=THIRD,
        does="Lists your files so Backblaze knows what to back up.",
        high="A scan, which is periodic and heavy on disk.",
        advice="Nothing. It finishes."),
}

# Suffixes and shapes that identify a process even when its exact name is not
# in the catalogue. Ordered: the first match wins.
PATTERNS = (
    (re.compile(r"Helper \(Renderer\)$"),
     dict(name="A browser tab", cat=BROWSER,
          does="Renders one tab or one web view inside an app built on "
               "Chromium -- a browser, or an app like Slack or VS Code.",
          high="A single heavy page. One tab can hold a core on its own.",
          advice="Close tabs and watch which one drops it.")),
    (re.compile(r"Helper \(GPU\)$"),
     dict(name="A browser's graphics helper", cat=BROWSER,
          does="Draws what a Chromium-based app puts on screen.",
          high="Video, animation, or hardware acceleration struggling.",
          advice="Disabling hardware acceleration in the app's settings often "
                 "fixes a persistent case.")),
    (re.compile(r"Helper( \(.*\))?$"),
     dict(name="A helper process", cat=THIRD,
          does="A background part of an application, doing work on its behalf.",
          high="Whatever the parent app asked of it.",
          advice="Look at the application it belongs to, not this.")),
    (re.compile(r"^com\.apple\."),
     dict(name="A macOS service", cat=APPLE,
          does="A background service that is part of macOS.",
          high="Usually short-lived work on behalf of an app.",
          advice="Nothing, unless it stays high for a long time.")),
)

# Weaker than the path. "weird" ends in a d and is not a daemon; a binary under
# /opt/homebrew is one thing we can say for certain. Consulted only after the
# path has had its turn.
WEAK = (
    (re.compile(r"d$"),
     dict(name="A background service", cat=APPLE,
          does="A daemon -- a program with no window that runs in the "
               "background. The trailing 'd' is the convention for one.",
          high="Depends entirely on what it does.",
          advice="Nothing on its own. Judge it by how long it stays busy.")),
)


def _from_path(cmdline):
    """A deduction from where the binary lives, when the name is unknown.

    Said as a deduction, because it is one. Claiming to know what an
    unrecognised process does would be exactly the confident wrong answer this
    file exists to avoid.
    """
    path = (cmdline or "").split(" ")[0]
    if path.startswith("/System/") or path.startswith("/usr/libexec/"):
        return dict(name="A macOS component", cat=APPLE,
                    does="Part of macOS. It lives inside the system, which "
                         "third-party software cannot write to.",
                    high="Depends on what it is for.",
                    advice="Nothing on its own.", guessed=True)
    if "/Applications/" in path:
        return dict(name="Part of an installed application", cat=THIRD,
                    does="It belongs to an app in your Applications folder.",
                    high="Whatever that app is doing.",
                    advice="Quit the app to stop it.", guessed=True)
    if path.startswith("/opt/homebrew/") or path.startswith("/usr/local/"):
        return dict(name="Something you installed yourself", cat=DEV,
                    does="Installed by Homebrew or by hand, rather than by "
                         "macOS or the App Store.",
                    high="Whatever it was started to do.",
                    advice="The command line says what it is running.",
                    guessed=True)
    return None


def describe(exe, cmdline="", app=""):
    """What this process is, as far as can honestly be said.

    Returns a dict with name, does, high and advice, plus `known` -- false when
    the answer is a deduction rather than knowledge, so the interface can say
    so instead of sounding certain.
    """
    name = os.path.basename(exe or "")
    entry = CATALOGUE.get(name)
    if entry:
        out = dict(entry)
        out["known"] = True
        out["process"] = name
        return out

    def shaped(entry):
        out = dict(entry)
        out["known"] = False
        out["process"] = name
        if app and app != name:
            out["does"] = out["does"] + " This one belongs to %s." % app
        return out

    for pattern, entry in PATTERNS:
        if pattern.search(name):
            return shaped(entry)

    guess = _from_path(cmdline)
    if guess:
        guess["known"] = False
        guess["process"] = name
        return guess

    for pattern, entry in WEAK:
        if pattern.search(name):
            return shaped(entry)

    return dict(name=app or name, cat=THIRD, known=False, process=name,
                does="Not something this knows about. It is a program running "
                     "on your Mac; the command line below is the best clue.",
                high="Judge it by how long it stays busy rather than by its "
                     "name.",
                advice="")


def known_count():
    return len(CATALOGUE)

"""The data the grader decides from: which commands are readers, which flags are blocked, what is critical."""

import re

SHELL_CHARS = set("|&;<>`$\n")  # there's no shell, so these could only mislead
READERS = {"ls", "cat", "head", "tail", "wc", "grep", "file", "stat", "du", "cd", "diff", "realpath", "shasum", "tree",
           "mdfind"}
LOOKUPS = {"which", "pgrep", "ps", "pwd", "date", "whoami", "uname", "sw_vers", "echo"}  # no file arguments
HELP_FLAGS = {"--help", "-h", "help", "--version", "-V", "version"}
GIT_READ = {"status", "log", "diff", "show", "blame", "ls-files", "rev-parse", "shortlog", "describe", "rev-list",
            "ls-tree", "show-ref"}
GIT_WRITE = {"add", "commit", "checkout", "switch", "restore", "stash", "branch", "mv", "rm", "tag", "fetch", "pull",
             "merge", "init"}
GIT_BRANCH_LIST = {"-a", "-r", "-v", "-vv", "--list", "--show-current", "--all", "--remotes"}
GIT_REFUSED = {"push", "reset", "clean", "rebase", "config", "remote", "filter-branch", "gc", "prune"}
FILE_WRITERS = {"mkdir", "touch", "cp", "mv"}
RUNNERS = {"python", "python3", "make"}
UV_OK = {"sync", "add", "remove", "lock", "run", "tree"}
NPM_OK = {"install", "ci", "test", "run"}
CRITICAL = {"sudo", "su", "doas", "diskutil", "dd", "mkfs", "newfs_apfs", "fdisk", "shutdown", "reboot", "halt",
            "launchctl", "csrutil", "spctl", "security", "tccutil", "nvram", "pmset", "systemsetup", "networksetup",
            "dscl", "passwd", "mail", "sendmail", "chown", "kextload", "softwareupdate", "scutil"}
BLOCKED_FLAGS = {
    "git": ("--output", "--ext-diff", "--textconv", "--exec", "--upload-pack", "--receive-pack",
            "--open-files-in-pager"),
    "tail": ("-f", "-F", "--follow", "--retry"),
    "grep": ("-f", "--file", "--exclude-from", "--include-from"),
    "find": ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"),
    "date": ("-s", "--set"),
    "sed": ("-i", "-I", "--in-place"),
    "pytest": ("-p", "-c", "--rootdir", "--basetemp", "--confcutdir", "--pdb", "--trace", "--pyargs",
               "--override-ini", "-o"),
}
GREP_EXCLUDES = ["--exclude=.env*", "--exclude=*token*", "--exclude=*secret*", "--exclude=*.db",
                 "--exclude=*credential*", "--exclude=id_rsa*", "--exclude=id_ed25519*", "--exclude=*.pem",
                 "--exclude=*.key", "--exclude=*.keychain*", "--exclude-dir=.git", "--exclude-dir=.venv*",
                 "--exclude-dir=__pycache__", "--exclude-dir=.ssh", "--exclude-dir=.gnupg", "--exclude-dir=.aws",
                 "--exclude-dir=Keychains", "--exclude-dir=Cookies", "--exclude-dir=Messages",
                 "--exclude-dir=browser-profile"]
UV_VALUE_FLAGS = {"--extra", "--with", "--project", "--python", "--group", "--directory", "--package", "--env-file",
                  "--with-requirements", "-p"}
OPEN_FLAGS = {"-R", "-g", "-j", "-n", "-F"}
OPEN_RISKY = {".app", ".command", ".sh", ".zsh", ".bash", ".terminal", ".tool", ".scpt", ".applescript", ".workflow",
              ".pkg", ".dmg", ".jar", ".action", ".webloc", ".url", ".inetloc", ".osax", ".scptd"}
LSOF_SIMPLE = {"-n", "-P", "-t", "-a", "-w", "-l", "-U"}
LSOF_VALUED = {"-i", "-p", "-c", "-u", "-s"}
PUBLISH = {("npm", "publish"), ("uv", "publish"), ("brew", "uninstall"), ("brew", "remove"), ("defaults", "write"),
           ("defaults", "delete"), ("crontab", "-r"), ("crontab", "-e")}
REACHES_OUT = {"ssh", "scp", "rsync", "sftp", "ftp", "gh", "twine", "nc", "telnet"}
OSASCRIPT_DANGER = re.compile(
    r"do shell script|keystroke|key code|\bMessages\b|\bMail\b|\bsend\b|delete|\bFinder\b|Terminal|"
    r"System (?:Settings|Preferences|Events.+(?:shut down|restart|log out|sleep))|administrator|password|"
    r"empty (?:the )?trash|\bmove\b|duplicate|make new|\bquit\b|run script|open location|URL|keychain|"
    r"clipboard|the clipboard|set the clipboard", re.I)

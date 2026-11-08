# SPDX-License-Identifier: MIT
"""The commands the READMEs, the installers' messages and the hints give the user (spec §4.21, §10.6, §11.3
test_ff_hygiene_commands; split from test_ff_hygiene, which is at the 600-line limit).

What the user is told to type must work from both PowerShells and over SSH:
- a documented `ssh [-t] frame <command>` passes the remote command as one quoted argument: PowerShell 7 expands
  an unquoted ~/... to the PC's profile folder before ssh.exe sees it;
- no bare `systemctl --user <verb>`: over SSH XDG_RUNTIME_DIR may be unset and systemctl cannot reach the user
  manager (spec decision 35), so the user gets `frameface_main.py start|restart`, which sets it;
- every frameface_main.py command is the whole decided command line, `python3 -I ~/frameface/frameface_main.py
  <command>` on the headset or `ssh frame "python3 -I ~/frameface/frameface_main.py <command>"` from the PC
  (~/frameface is not on PATH); a bare subcommand (run `url --new-token`, run init-config) is no command either.
Docstrings, the program's own usage synopses and log lines that describe a running process are not commands.
Every text is whitespace-joined first, so a command that wraps over lines is checked as one command.
"""
import ffutil  # first: installs the port guard

import argparse
import ast
import re
import unittest

ffutil.setup_path()
from frameface.cli import build_parser, url_lines  # noqa: E402
from frameface.config import Config  # noqa: E402

REPO = ffutil.REPO
FF = ffutil.FRAMEFACE_DIR
CLI = "python3 -I ~/frameface/frameface_main.py"
READMES = ("README.md", "headset/frameface/README.md")
PS1, SH = "headset/frameface/install-frameface.ps1", "headset/frameface/install.sh"
SSH_FRAME = re.compile(r"\bssh(?: -t)? frame\b(.*)")
QUOTED_ARG = re.compile(r'"[^"]*"|\'[^\']*\'')
# Every systemctl command that talks to the user manager; a fine command carries the XDG_RUNTIME_DIR= prefix, and
# a report that a command failed is no instruction.
VERBS = ("start", "stop", "restart", "try-restart", "reload", "reload-or-restart", "try-reload-or-restart", "enable",
         "disable", "reenable", "status", "is-active", "is-enabled", "is-failed", "show", "show-environment", "cat",
         "daemon-reload", "kill", "mask", "unmask", "reset-failed", "edit", "list-units", "list-unit-files", "link",
         "isolate", "set-environment", "unset-environment", "import-environment")
SYSTEMCTL = re.compile(r"systemctl --user (?:%s)(?![\w-])" % "|".join(sorted(VERBS, key=len, reverse=True)))
XDG_PREFIX = re.compile(r"XDG_RUNTIME_DIR=(?:\$\([^)]*\)|\S)+ $")
FAILED = re.compile(r"(?: \S+)? failed\b")
# frameface_main.py's subcommands, from its own parser
SUBCOMMANDS = sorted(next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)).choices,
                     key=len, reverse=True)
_SUB = "|".join(re.escape(name) for name in SUBCOMMANDS)
MAIN_SUB = re.compile(r"frameface_main\.py (?:%s)(?![\w-])" % _SUB)          # needs "python3 -I ~/frameface/" before
BARE_SUB = re.compile(r"\b[Rr]un:? `(?:%s)(?![\w-])" % _SUB)                 # run `url --new-token`
# ... and without backticks (run init-config); not "on"/"off": "still run on the Steam Frame" is prose
BARE_RUN = re.compile(r"\b[Rr]un:? (?:%s)(?![\w-])" % "|".join(re.escape(s) for s in SUBCOMMANDS if s not in ("on", "off")))
UNQUOTED_REMOTE = re.compile(r"\bssh(?: -t)? [^\s\"'`]+ " + re.escape(CLI))  # ssh frame python3 -I ~/...
PROCESS = re.compile(r"(?: --?[\w-]+)* (?:is running|\(pid)\b")             # "frameface_main.py run is running"


def read_text(rel):
    """A repository file as text with LF line ends (a Windows checkout may have CRLF)."""
    return (REPO / rel).read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")


def flat(text):
    return " ".join(text.split())


def code_spans(markdown):
    """Every line of a fenced code block, then every inline code span (as one line, even where it wraps)."""
    lines = []

    def fence(match):
        lines.extend(match.group(1).splitlines())
        return ""
    rest = re.sub(r"(?ms)^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", fence, markdown)
    return lines + [flat(span) for span in re.findall(r"`([^`]+)`", rest)]


def unquoted_ssh(markdown):
    """The `ssh [-t] frame <command>` examples whose remote command is not one quoted argument."""
    return [span for span in code_spans(markdown) for m in SSH_FRAME.finditer(span)
            if m.group(1).strip() and not QUOTED_ARG.fullmatch(m.group(1).strip())]


def snippet(text, m):
    return text[max(0, m.start() - 40):m.end() + 30]


def bare_systemctl(text):
    """The `systemctl --user <verb>` commands that text gives the user without the XDG_RUNTIME_DIR= prefix."""
    text = flat(text)
    return [snippet(text, m) for m in SYSTEMCTL.finditer(text)
            if not XDG_PREFIX.search(text, 0, m.start()) and not FAILED.match(text, m.end())]


def partial_commands(text):
    """frameface_main.py commands that are not the whole decided command line, bare subcommands given as
    commands, and the CLI after an ssh target without quotes."""
    text = flat(text)
    found = [snippet(text, m) for m in MAIN_SUB.finditer(text)
             if not text[:m.start()].endswith("python3 -I ~/frameface/") and not PROCESS.match(text, m.end())]
    return found + [snippet(text, m) for regex in (BARE_SUB, BARE_RUN, UNQUOTED_REMOTE) for m in regex.finditer(text)]


def python_strings(source):
    """The string constants of Python source (implicitly concatenated literals as one string), except docstrings:
    notes for developers, never shown to the user."""
    tree = ast.parse(source)
    docs = {id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body
            and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


def shell_messages(script):
    """The double-quoted strings of a bash script, i.e. its messages, not the commands it runs itself. Comments and
    single-quoted strings are skipped; a $(...) inside a message belongs to it, with its own quotes."""
    found, i, n = [], 0, len(script)
    while i < n:
        c = script[i]
        if c == "\\":
            i += 2
        elif c == "'":
            end = script.find("'", i + 1)
            i = n if end < 0 else end + 1
        elif c == "#" and (i == 0 or script[i - 1] in " \t\n;|&("):
            end = script.find("\n", i)
            i = n if end < 0 else end
        elif c == '"':
            j, depth = i + 1, 0
            while j < n and (script[j] != '"' or depth):
                if script[j] == "\\":
                    j += 1
                elif script.startswith("$(", j):
                    depth, j = depth + 1, j + 1
                elif script[j] == ")" and depth:
                    depth -= 1
                j += 1
            found.append(script[i + 1:j])
            i = j + 1
        else:
            i += 1
    return found


def frameface_modules():
    modules = sorted(p for p in FF.rglob("*.py") if "__pycache__" not in p.parts)
    assert len(modules) > 30, modules
    return modules


class UserFacingCommands(unittest.TestCase):
    """What the READMEs, the hints and the installers' messages tell the user to type works from both
    PowerShells and over SSH."""

    def test_the_matchers(self):
        self.assertEqual(unquoted_ssh("or `ssh frame python3 -I ~/x.py url`."), ["ssh frame python3 -I ~/x.py url"])
        self.assertEqual(unquoted_ssh("```powershell\nssh -t frame python3 x\n```\n"), ["ssh -t frame python3 x"])
        self.assertEqual(unquoted_ssh('`ssh frame "a ~/b"`, `ssh -t frame \'c\'`, `ssh frame`, `ssh frame\n"wrapped c"`\n'
                                      '```\nssh -t frame "x y"\n```\n'), [])
        for text in ("then `systemctl --user restart frameface`;", 'ssh frame "systemctl --user start frameface.service"',
                     "systemctl --user stop frameface", "systemctl --user try-restart frameface",
                     'ssh frame "systemctl --user enable --now frameface"', "Restart it: `systemctl --user\nrestart x`.",
                     "systemctl --user status frameface", "systemctl --user daemon-reload"):
            self.assertEqual(len(bare_systemctl(text)), 1, text)
        for text in ("XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user start frameface.service",
                     "[FAIL] systemctl --user restart frameface.service failed (journalctl --user -u frameface)",
                     "systemctl --user daemon-reload failed", "systemctl --user %s", "systemctl --user does not work",
                     "checks `systemctl --user`, `gcc`"):
            self.assertEqual(bare_systemctl(text), [], text)
        script = ('systemctl --user restart "$SVC" || fail "x"  # "a comment: systemctl --user start x"\n'
                  "  echo 'single: systemctl --user stop x'\n  info \"start it with: systemctl --user start $SVC\"\n"
                  '  warn "the \\"$(id -u "$U")\\" user: systemctl --user\n    restart $SVC" ; fail "systemctl --user '
                  'enable $SVC failed"\n')
        self.assertEqual([m for m in shell_messages(script) if bare_systemctl(m)],
                         ["start it with: systemctl --user start $SVC",
                          'the \\"$(id -u "$U")\\" user: systemctl --user\n    restart $SVC'])
        source = '"""module: not shown"""\nclass C:\n    """class: not shown"""\n    def f(self):\n        """f: not ' \
                 'shown"""\n        print("printed: " "joined", f"x{1}y")\n'
        self.assertEqual(sorted(python_strings(source)), ["printed: joined", "x", "y"])
        for text in ("Run `frameface_main.py on` on the headset", "from `frameface_main.py url`",
                     "(frameface_main.py url prints it)", "`frameface_main.py restart`", "~/frameface/frameface_main.py on",
                     "python3 ~/frameface/frameface_main.py init-config", "run `url --new-token` (and update",
                     "Run: `status`", "ssh frame %s url" % CLI, "ssh -t steamos@frame %s probe --active" % CLI,
                     "url: no readable %s; run init-config first", "(run init-config --new-token)"):
            self.assertEqual(len(partial_commands(text)), 1, text)
        for text in ("`%s\nrestart`" % CLI, 'ssh frame "%s url --new-token"' % CLI, "start it with: %s start" % CLI,
                     "run: ssh {1} \"%s start\"" % CLI, "`%s <command>`" % CLI, "`frameface_main.py online`",
                     "(run %s init-config --new-token)" % CLI, "run %s init-config first", "a run of selftests",
                     "the user bus is unreachable and frameface_main.py run is running (pid %s)",
                     "the eye-data reader still run on the Steam Frame", "runs `selftest --quick` and starts",
                     "`probe --active` characterises the hardware", '"$CUR/frameface_main.py" status'):
            self.assertEqual(partial_commands(text), [], text)
        self.assertEqual(sorted(SUBCOMMANDS), ["init-config", "off", "on", "probe", "restart", "run", "selftest", "sim",
                                               "start", "status", "url"])

    def test_ssh_examples_pass_the_remote_command_as_one_quoted_argument(self):
        for rel in READMES:
            text = read_text(rel)
            self.assertIn("`ssh frame", text, rel)
            self.assertEqual(unquoted_ssh(text), [], rel)

    def test_no_bare_systemctl_is_given_to_the_user(self):
        found = {rel: bare_systemctl(read_text(rel)) for rel in READMES + (PS1,)}
        found[SH] = [hit for message in shell_messages(read_text(SH)) for hit in bare_systemctl(message)]
        # every module: the display hints of policy.py, the probe's HINTS, cli.py's messages, wherever they live
        for path in frameface_modules():
            found[path.relative_to(REPO).as_posix()] = [hit for text in python_strings(path.read_bytes())
                                                         for hit in bare_systemctl(text)]
        self.assertEqual({rel: hits for rel, hits in found.items() if hits}, {})

    def test_every_frameface_command_is_the_whole_command_line(self):
        # A bare `frameface_main.py on` or `url --new-token` runs nothing (~/frameface is not on PATH): the user
        # always gets the CLI, on the headset or quoted after `ssh frame`. Python: every string of every module
        # (policy.DISPLAY and its fallbacks, statusdoc, the probe's hints, cli.py's messages), docstrings aside.
        found = {rel: partial_commands(read_text(rel)) for rel in READMES + (PS1, SH)}
        modules = frameface_modules()
        self.assertLessEqual({"policy.py", "statusdoc.py", "probe.py", "probe_active.py", "cli.py"}, {p.name for p in modules})
        for path in modules:
            found[path.relative_to(REPO).as_posix()] = [hit for text in python_strings(path.read_bytes())
                                                         for hit in partial_commands(text)]
        self.assertEqual({rel: hits for rel, hits in found.items() if hits}, {})

    def test_readme_port_in_use_row_is_the_display_hint(self):
        from frameface.policy import DISPLAY
        row = re.search(r"(?m)^\| `port_in_use` \| (.+?) \| (.+?) \|$", read_text("README.md"))
        self.assertIn("then `%s restart`; Baballonia's address changes with it" % CLI, row.group(2))
        plain = [text.replace("`", "") for text in row.groups()]
        self.assertEqual(plain, [text.replace("`", "") for text in DISPLAY["port_in_use"][1:]])     # the §4.18 row

    def test_restart_and_lan_url_instructions(self):
        readme, ff = flat(read_text("README.md")), flat(read_text("headset/frameface/README.md"))
        for phrase in ('ssh frame "%s url"' % CLI, '`ssh frame "%s url --new-token"`' % CLI,
                       '`ssh frame "%s restart"`' % CLI, "restarts a running service itself",
                       "Run `%s on` on the headset" % CLI):
            self.assertIn(phrase, readme)
        for phrase in ("`%s restart`" % CLI, "| `restart` / `start` |", "restarts a running service itself",
                       "| `-AllowPeer <CIDR>[,<CIDR>...]` |", "one or more IPv4 CIDRs, comma-separated",
                       "e.g. `192.168.4.31/32`", "overlaps the headset's wlan0 network",
                       "| `disabled` | face tracking is off: `%s on` |" % CLI):
            self.assertIn(phrase, ff)
        self.assertIn('info "not started (--no-start); start it with: %s start"' % CLI, read_text(SH))
        self.assertIn("run: ssh {1} \"%s start\"'" % CLI, read_text(PS1))

    def test_the_lan_url_rule_is_documented_as_url_applies_it(self):
        # url prints the LAN line for a CIDR that is not loopback and not inside the PC link, even one that contains
        # the link (10.0.0.0/8): "outside the PC link" would be wrong for it.
        self.assertIn("when an `allow_peers` CIDR that is not loopback and not inside the PC link `10.35.78.0/24` "
                      "overlaps the headset's wlan0 network", flat(read_text("headset/frameface/README.md")))
        token = "AbCdEfGhIjKlMnOpQrStUv"
        for extra, wlan0, lines in ((["10.0.0.0/8"], "10.20.30.40/24", 2), (["0.0.0.0/0"], "192.168.4.203/24", 2),
                                    (["192.168.4.31/32"], "192.168.4.203/24", 2), (["10.35.78.0/25"], "10.35.78.9/24", 1),
                                    (["127.0.0.0/16"], "127.0.0.9/8", 1), (["192.168.5.0/24"], "192.168.4.203/24", 1)):
            got = url_lines(Config(token=token, allow_peers=["10.35.78.0/24", "127.0.0.0/8"] + extra), wlan0)
            self.assertEqual(len(got), lines, (extra, wlan0, got))


if __name__ == "__main__":
    unittest.main()

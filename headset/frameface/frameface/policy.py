# SPDX-License-Identifier: MIT
"""Every recovery and idle decision of the service as pure functions (spec §4.7, §4.13, §4.14,
§4.18): the strategy ladder and XU length, the failure table, backoff and settle, the run/idle
truth table, brown-out and IR levels, the stream token bucket, CPU lists and the display table.
"""
from dataclasses import dataclass
import enum
import math
import re
import _thread

__all__ = [
    "Strategy", "STRATEGIES", "LADDER", "ladder", "xu_len_for", "Failure", "FailureContext", "Decision",
    "FAULT_RETRY_S", "RESET_GAP_S", "on_failure", "backoff_s", "settle_deadline", "AbsenceTracker", "RunInputs",
    "RunDecision", "run_decision", "BrownoutTracker", "IR_LEVELS", "ir_next", "TokenBucket", "parse_cpus", "DISPLAY",
    "FAULT_DISPLAY", "display",
]


# -- strategies (§4.7)

@dataclass(frozen=True)
class Strategy:
    name: str
    si_before: bool
    si_after: bool
    xu_between: bool
    rhythm: bool
    si_mid: bool = False                # SETINTERFACE(1,0) after Block A, before the probe (S5)
    set_config: bool = False            # USBDEVFS_SETCONFIGURATION before CLAIM (S5)
    xu_len: "int | None" = None         # None = GET_LEN; 384 for S5
    stream_ack_poll: bool = True        # False: STREAM_OFF/ON sent without GET_CUR polls (S5)


STRATEGIES = {
    "S0": Strategy("S0", si_before=True, si_after=True, xu_between=False, rhythm=False),
    "S1": Strategy("S1", si_before=True, si_after=False, xu_between=False, rhythm=False),
    "S2": Strategy("S2", si_before=False, si_after=True, xu_between=False, rhythm=False),
    "S3": Strategy("S3", si_before=True, si_after=True, xu_between=True, rhythm=False),
    "S4": Strategy("S4", si_before=True, si_after=True, xu_between=False, rhythm=True),
    "S5": Strategy("S5", si_before=False, si_after=False, xu_between=False, rhythm=False, si_mid=True,
                   set_config=True, xu_len=384, stream_ack_poll=False),
}
LADDER = ("S0", "S1", "S2", "S3", "S4", "S5")


def ladder(known_good: "str | None", pinned: "str | None") -> list:
    """The rungs of one ladder pass: [pinned] if a strategy is pinned (not "auto"); else the
    known-good rung first (the caller passes it only if the descriptor SHA-1 matches), then
    S0..S5 without duplicates. Unknown names are ignored."""
    if pinned in STRATEGIES:
        return [pinned]
    first = [known_good] if known_good in STRATEGIES else []
    return first + [s for s in LADDER if s not in first]


def _numeric_xu_len(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value in (64, 384):
        return value
    if isinstance(value, str) and value.strip() in ("64", "384"):
        return int(value)
    return None


def xu_len_for(rung: str, cfg_xu_len, known_good: "str | None", profile_xu_len: "int | None",
               get_len: int) -> int:
    """XU command length of one activation (§4.5): config 64/384 > the known-good pair (this rung is
    the profile's strategy and its xu_len is set) > the strategy's own length (S5: 384) > GET_LEN."""
    n = _numeric_xu_len(cfg_xu_len)
    if n is not None:
        return n
    if known_good is not None and rung == known_good and profile_xu_len:
        return int(profile_xu_len)
    strategy = STRATEGIES.get(rung)
    if strategy is not None and strategy.xu_len:
        return strategy.xu_len
    return int(get_len)


# -- failures (§4.13)

class Failure(str, enum.Enum):
    XU_TIMEOUT = "xu_timeout"
    XU_ERROR = "xu_error"
    PROBE_REJECT = "probe_reject"
    NO_DATA = "no_data"
    NO_FRAMES = "no_frames"
    TEST_BARS = "test_bars"
    DARK = "dark"
    STALL = "stall"
    FROZEN = "frozen"
    EPIPE_STORM = "epipe_storm"
    XFER_STORM = "xfer_storm"
    LOST = "lost"
    PERMISSION = "permission"
    BUSY = "busy"
    UNSUPPORTED = "unsupported"
    LIBRARY = "library"

    def __str__(self):                  # "dark" in logs on every Python version (3.11 changed str/format)
        return self.value

    __format__ = str.__format__


@dataclass
class FailureContext:
    strategy: str
    tried: set
    consecutive_xu: int
    good_stream_s: float
    soft_reopens_in_row: int
    direct_port: bool
    allow_reset: bool
    last_reset_age_s: float
    reset_allowance: int
    ladder_restarts: int
    seen_ok: bool = False               # an ok frame in this activation (or in the one an active IR trial replaced)
    brownout_pending: bool = False      # >= 1 brown-out suspect within the last 600 s
    ir_trial_possible: bool = False     # session IR level < L3, no trial active, dark actions not suppressed
    ladder: "tuple | None" = None       # the rungs of this pass in order (None = S0..S5; [pinned] when pinned)


@dataclass(frozen=True)
class Decision:
    action: str     # retry_same | next_strategy | reset_port | ir_trial | suppress | fault | wait_device
    settle_mult: float = 1.0
    delay_s: float = 0.0                # fault: retry delay (math.inf = never)
    hint: str = ""
    fault: str = ""                     # permission | busy | unsupported | library | wedged | no_ir
    strategy: str = ""                  # rung of the next activation ("" for fault, suppress, wait_device)
    row: int = 0                        # the §4.13 row that decided


FAULT_RETRY_S = {"permission": 30.0, "busy": 30.0, "unsupported": 60.0, "library": math.inf, "wedged": 60.0,
                 "no_ir": 600.0, "port": 30.0}
RESET_GAP_S = 600.0                     # §4.12: no USB port reset within 600 s of the last one (rows 6 and 13)
_XU = (Failure.XU_TIMEOUT, Failure.XU_ERROR)
_SOFT = (Failure.STALL, Failure.FROZEN, Failure.EPIPE_STORM, Failure.XFER_STORM)
_RESET_ALLOWANCE = (Failure.XU_TIMEOUT, Failure.XU_ERROR, Failure.NO_DATA, Failure.NO_FRAMES, Failure.TEST_BARS,
                    Failure.DARK)
DARK_START_HINT = "the tracker's IR light did not switch on; trying %s"


def _fault(kind, row):
    name = FAULT_DISPLAY[kind]
    return Decision("fault", delay_s=FAULT_RETRY_S[kind], hint=_fill(name, DISPLAY[name][2], {}), fault=kind,
                    row=row)


def on_failure(f: Failure, ctx: FailureContext) -> Decision:
    """The §4.13 table, strictly top to bottom. Dark after ok frames (or with a brown-out suspect
    pending) is an IR question (rows 4-5); dark from the first frame is routed like NO_FRAMES, with
    one final IR trial (row 14) and fault no_ir (row 15). An XU failure whose next_strategy finds no
    untried rung escalates like row 9. Both reset rows (6 and 13) keep §4.12's 600 s between resets;
    row 6 otherwise falls through and its allowance stays for later."""
    f = Failure(f)
    rungs = tuple(ctx.ladder) if ctx.ladder else LADDER
    untried = [r for r in rungs if r not in ctx.tried]
    if f is Failure.LOST:
        return Decision("wait_device", row=1)
    if f in (Failure.PERMISSION, Failure.BUSY):
        return _fault(f.value, 2)
    if f in (Failure.UNSUPPORTED, Failure.LIBRARY):
        return _fault(f.value, 3)
    if f is Failure.DARK and (ctx.seen_ok or ctx.brownout_pending):
        if ctx.ir_trial_possible:
            return Decision("ir_trial", strategy=ctx.strategy, row=4)
        return Decision("suppress", hint=DISPLAY["dark"][2], row=5)
    if ctx.reset_allowance > 0 and f in _RESET_ALLOWANCE and ctx.direct_port and ctx.allow_reset and \
            ctx.last_reset_age_s >= RESET_GAP_S:
        return Decision("reset_port", strategy=ctx.strategy, row=6)
    if f in _XU:
        if ctx.consecutive_xu == 1:
            return Decision("retry_same", settle_mult=2.0, strategy=ctx.strategy, row=7)
        if ctx.consecutive_xu == 2 and untried:
            return Decision("next_strategy", strategy="S4" if "S4" in untried else untried[0], row=8)
        # row 9 (consecutive_xu >= 3, or no rung left for row 8): escalate to row 13
    else:
        if f in _SOFT and ctx.soft_reopens_in_row < 3:
            return Decision("retry_same", strategy=ctx.strategy, row=10)
        if ctx.good_stream_s >= 60:
            return Decision("retry_same", strategy=ctx.strategy, row=11)
        if untried:
            nxt = untried[0]
            return Decision("next_strategy", strategy=nxt, hint=DARK_START_HINT % nxt if f is Failure.DARK else "",
                            row=12)
    if ctx.direct_port and ctx.allow_reset and ctx.last_reset_age_s >= RESET_GAP_S and ctx.ladder_restarts == 0:
        return Decision("reset_port", strategy=rungs[0], row=13)
    if f is Failure.DARK and ctx.ir_trial_possible:
        return Decision("ir_trial", strategy=rungs[0], row=14)
    return _fault("no_ir" if f is Failure.DARK else "wedged", 15)


_BACKOFF = (1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 10.0, 30.0, 60.0)


def backoff_s(consecutive: int) -> float:
    """Backoff after the consecutive-th failure in a row (1-based): 1, 1.5, 2, 3, 4, 5, 10, 30, 60, 60, ..."""
    return _BACKOFF[min(max(int(consecutive), 1), len(_BACKOFF)) - 1]


def settle_deadline(now_bt: float, *, same_boot: bool, enum_bt: float, start_bt: float, dirty_start: bool,
                    last_teardown_bt: "float | None", last_reset_bt: "float | None", absent_since_teardown: bool,
                    settle_s: float, enum_settle_s: float, settle_mult: float, backoff_until_bt: float) -> float:
    """CLOCK_BOOTTIME before which no activation may start (§4.13): the enumeration settle and the
    backoff always; within the same boot also settle_s after a reset (never skipped), settle_s *
    settle_mult after the last teardown (skipped after a real absence) and settle_s after a dirty
    start. The service passes the end of its last session as `last_teardown_bt`: a clean teardown or
    a LOST, whichever came later (a LOST while the tracker stays attached needs the settle too).
    `now_bt` is not needed for the deadline; settle_ok = now_bt >= the result."""
    d = max(enum_bt + enum_settle_s, backoff_until_bt)
    if same_boot:
        if last_reset_bt is not None:
            d = max(d, last_reset_bt + settle_s)
        if last_teardown_bt is not None and not absent_since_teardown:
            d = max(d, last_teardown_bt + settle_s * settle_mult)
        if dirty_start:
            d = max(d, start_bt + settle_s)
    return d


class AbsenceTracker:
    """Feeds the 1 Hz presence scans and answers `absent_since_teardown` of §4.13: the device was
    missing in >= 2 consecutive scans at or after the last teardown, in an absence that did not start
    within 30 s after our own last reset (an unplug or brown-out power-cycles the tracker; a reset
    of ours does not count). An absence whose first missing scan came just before the teardown
    still counts by its later scans (the scan can see an unplug before the LOST path runs)."""

    RESET_GRACE_S = 30.0

    def __init__(self):
        self._start = self._last = None
        self._missing = 0
        self.absences = []              # [start, second-last missing scan] of absences with >= 2 missing scans
                                        # (last 16)

    def scan(self, now_bt: float, present: bool) -> None:
        if present:
            self._start, self._last, self._missing = None, None, 0
            return
        if self._start is None:
            self._start = now_bt
        self._missing += 1
        if self._missing == 2:
            self.absences.append([self._start, self._last])
            del self.absences[:-16]
        elif self._missing > 2:
            self.absences[-1][1] = self._last
        self._last = now_bt

    def absent_since(self, last_teardown_bt: "float | None", last_reset_bt: "float | None") -> bool:
        for start, second_last in self.absences:
            if last_teardown_bt is not None and second_last < last_teardown_bt:    # < 2 missing scans after it
                continue
            if last_reset_bt is not None and last_reset_bt <= start < last_reset_bt + self.RESET_GRACE_S:
                continue
            return True
        return False


# -- idle and wake (§4.14)

@dataclass(frozen=True)
class RunInputs:
    enabled: bool
    present: bool
    battery_low: bool
    worn: "bool | None"
    clients: int
    streaming: bool
    now: float
    last_client_t: float                # monotonic time a client was last connected (None/-inf: never)
    unworn_since: "float | None"
    settle_ok: bool


@dataclass(frozen=True)
class RunDecision:
    usb: str                            # start | keep | stop | idle
    publish: bool
    why: str                            # "" | disabled | no_device | battery | not_worn | no_clients | settling


def run_decision(i: RunInputs, cfg) -> RunDecision:
    """The §4.14 truth table; cfg supplies power_policy, gate_on_worn, no_client_linger_s and
    unworn_linger_s."""
    if not i.enabled:
        return RunDecision("stop" if i.streaming else "idle", False, "disabled")
    if not i.present:
        return RunDecision("idle", False, "no_device")
    if i.battery_low:
        return RunDecision("stop" if i.streaming else "idle", False, "battery")
    always = cfg.power_policy == "always"
    worn_gate = bool(cfg.gate_on_worn) and i.worn is False
    last = -math.inf if i.last_client_t is None else i.last_client_t
    client_recent = i.clients > 0 or i.now - last < cfg.no_client_linger_s
    unworn_long = worn_gate and i.unworn_since is not None and i.now - i.unworn_since >= cfg.unworn_linger_s
    want = True if always else (client_recent and not unworn_long)
    publish = i.streaming and i.clients > 0 and not worn_gate
    can_start = False
    if i.streaming:
        usb = "keep" if want else "stop"
    else:
        can_start = want and (always or (i.clients > 0 and not worn_gate))
        usb = "start" if can_start and i.settle_ok else "idle"
    if worn_gate:
        why = "not_worn"
    elif not client_recent:
        why = "no_clients"
    elif can_start and not i.settle_ok:
        why = "settling"
    else:
        why = ""
    return RunDecision(usb, publish, why)


# -- brown-out and IR (§4.13)

class BrownoutTracker:
    """Brown-out suspects (B1: LOST within 30 s after an activation; B2: > 50 transfer errors in
    1 s). suspect() is True when this is the second suspect within window_s (then the count starts
    over); pending() is True while any suspect lies within the last window_s (FailureContext
    .brownout_pending, §4.13), including the two that just lowered the level."""

    def __init__(self, window_s: float = 600.0):
        self.window_s = window_s
        self.total = 0
        self.last_reason = ""
        self._times = []                # suspects counted towards the next "two within window_s"
        self._recent = []               # every suspect of the last window_s (pending)

    def suspect(self, now: float, reason: str) -> bool:
        self.total += 1
        self.last_reason = reason
        self._recent = [t for t in self._recent if now - t <= self.window_s] + [now]
        self._times = [t for t in self._times if now - t <= self.window_s]
        self._times.append(now)
        if len(self._times) >= 2:
            self._times = []
            return True
        return False

    def pending(self, now: float) -> bool:
        return any(now - t <= self.window_s for t in self._recent)


IR_LEVELS = {"L0": 0xB2, "L1": 0x96, "L2": 0x78, "L3": 0x5A}     # current registers 0x05-0x07 (0.1 mA steps)


def ir_next(level: str) -> "str | None":
    """The next lower IR current level, or None at L3 (or for an unknown level)."""
    order = list(IR_LEVELS)
    if level not in IR_LEVELS or level == order[-1]:
        return None
    return order[order.index(level) + 1]


class TokenBucket:
    """Shared byte budget of all stream clients: `rate_bytes_s` with a `burst` capacity. An item
    larger than the burst passes when the bucket is full and leaves a debt, so the long-run rate
    holds for any item size. allow() is atomic (one _thread lock), callable from every client."""

    def __init__(self, rate_bytes_s: float, burst: int):
        if rate_bytes_s <= 0 or burst <= 0:
            raise ValueError("rate and burst must be positive")
        self.rate = float(rate_bytes_s)
        self.burst = int(burst)
        self._tokens = float(burst)
        self._t = None
        self._lock = _thread.allocate_lock()

    def allow(self, now: float, n: int) -> bool:
        with self._lock:
            if self._t is None or now > self._t:
                if self._t is not None:
                    self._tokens = min(float(self.burst), self._tokens + (now - self._t) * self.rate)
                self._t = now
            if self._tokens >= min(n, self.burst):
                self._tokens -= n
                return True
            return False


_CPU_PART = re.compile(r"^(\d{1,4})(?:-(\d{1,4}))?$")
MAX_CPU = 1023


def parse_cpus(s: str) -> "set | None":
    """"auto" -> None (no pinning); "0-1,4" -> {0, 1, 4}; anything else -> ValueError."""
    if not isinstance(s, str):
        raise ValueError("cpu list must be a string, not %r" % (s,))
    if s.strip() == "auto":
        return None
    out = set()
    for part in s.split(","):
        m = _CPU_PART.match(part.strip())
        if not m:
            raise ValueError("invalid cpu list %r" % (s,))
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) is not None else lo
        if lo > hi or hi > MAX_CPU:
            raise ValueError("invalid cpu range %r in %r" % (part.strip(), s))
        out.update(range(lo, hi + 1))
    return out


# -- display table (§4.18): name -> (severity, message, hint); <x> are filled from the context

DISPLAY = {
    "disabled": ("info", "Face tracking is off", "Run `python3 -I ~/frameface/frameface_main.py on` on the headset"),
    "no_device": ("info", "Tracker not plugged in",
                  "Plug the VIVE Facial Tracker straight into the Frame's USB-C port (no hub, no charger)"),
    "port_device_mode": ("warn", "USB-C port is in charging/PC mode",
                         "Unplug the charger or PC cable, then plug in the tracker; it cannot run while the "
                         "headset charges"),
    "no_permission": ("error", "No permission to use the tracker",
                      'See "udev fallback" in headset/frameface/README.md (one sudo step)'),
    "busy": ("error", "Tracker used by another program",
             "Stop <owner> (vftstream, Babble Frame Runtime, vive-face-init)"),
    "standby": ("info", "Paused: headset not worn",
                'Starts again when you put the headset on. "Worn" means the eye tracker\'s sample counter is '
                'moving; if you wear it and eye tracking is off, set `"gate_on_worn": false` in '
                '`~/.config/frameface/config.json`'),
    "waiting_client": ("info", "Ready, waiting for Baballonia",
                       'Face Camera Address <url>, "This is a..." = Wireless/IP Camera'),
    "settling": ("info", "Tracker settling (<n> s)", ""),
    "starting": ("info", "Starting the tracker", ""),
    "streaming": ("ok", "Streaming <f> fps to <n> client(s)", ""),
    "recovering": ("warn", "Tracker stalled, restarting", ""),
    "test_pattern": ("warn", "Tracker shows test bars, trying <S>", "If it persists, unplug the tracker for 10 s"),
    "dark_start": ("warn", "Tracker image black, trying <S>",
                   "The IR light did not switch on with the last start sequence; FrameFace tries the next one "
                   "by itself"),
    "dark": ("warn", "Tracker image is black",
             "It was bright before: is the tracker facing your mouth, or is the lens covered? If it persists, "
             "unplug it for 10 s"),
    "brownout": ("warn", "Tracker lost power, IR lowered to <L>",
                 "Use a direct C-to-C connection; if it keeps happening unplug it for 5-10 min or reboot the "
                 "headset"),
    "wedged": ("error", "Tracker does not respond", "Unplug the tracker for 10 s and plug it back in"),
    "no_ir": ("error", "Tracker IR light did not switch on",
              "Every start sequence gave a black image. Unplug the tracker for 10 s and plug it back in; if it "
              "stays black run install-frameface.ps1 -ProbeActive and report the bundle"),
    "port_in_use": ("error", "Face stream port <p> is in use",
                    "Stop <owner> or set another `http_port` in `~/.config/frameface/config.json`, then "
                    "`python3 -I ~/frameface/frameface_main.py restart`; Baballonia's address changes with it"),
    "unsupported": ("error", "Unsupported tracker or system", "Run install-frameface.ps1 -Probe and report the bundle"),
    "library": ("error", "libturbojpeg is missing", "Update SteamOS, then run install.sh again"),
}
FAULT_DISPLAY = {"permission": "no_permission", "busy": "busy", "unsupported": "unsupported", "library": "library",
                 "wedged": "wedged", "no_ir": "no_ir", "port": "port_in_use"}
HUB_HINT = "connected through a hub; connect it directly if it drops out"
SWAR_HINT = "reduced frame rate (python pixels): run install.sh again"
_STARTING = ("INIT", "OPENING", "ACTIVATING", "NEGOTIATING", "STARTING")
_OVERLAYS = ("test_pattern", "dark", "brownout")


def _recovery(ctx):
    last = ctx.get("last_failure")
    if last in ("test_bars", "test_pattern"):
        return "test_pattern"
    if last == "dark_start":
        return "dark_start"
    return "recovering" if last else None


def _display_name(state, ctx):
    if state == "DISABLED":
        return "disabled"
    if state == "FAULT":
        return FAULT_DISPLAY.get(ctx.get("fault"), "wedged")
    if state in ("NO_DEVICE", "LOST"):
        return "port_device_mode" if ctx.get("data_role") == "device" else "no_device"
    if state == "IDLE":
        return "standby" if ctx.get("why") == "not_worn" else "waiting_client"
    recovery = _recovery(ctx)
    if state in ("SETTLING", "BACKOFF"):                # a brown-out's hint also while waiting for the next start
        return "brownout" if ctx.get("overlay") == "brownout" else (recovery or "settling")
    if state in _STARTING:
        return recovery if recovery in ("test_pattern", "dark_start") else "starting"
    if state == "STREAMING":
        return ctx.get("overlay") if ctx.get("overlay") in _OVERLAYS else "streaming"
    if state in ("TEARDOWN", "RESETTING"):
        if ctx.get("fault") in FAULT_DISPLAY:
            return FAULT_DISPLAY[ctx["fault"]]
        if recovery or state == "RESETTING":
            return recovery or "recovering"
        return {"disabled": "disabled", "not_worn": "standby"}.get(ctx.get("why"), "waiting_client")
    return "starting"


def _fill(name, text, ctx):
    if name == "settling":
        n = int(math.ceil(max(0.0, float(ctx.get("settle_left_s") or 0))))
    else:
        n = int(ctx.get("clients") or 0)
    values = {
        "<owner>": str(ctx.get("owner") or "the other program"),
        "<url>": str(ctx.get("url") or "from `python3 -I ~/frameface/frameface_main.py url`"),
        "<n>": str(n),
        "<f>": str(int(round(float(ctx.get("fps") or 0)))),
        "<S>": str(ctx.get("next_rung") or "again"),
        "<L>": str(ctx.get("ir_level") or "the next level"),
        "<p>": str(ctx["port"] if ctx.get("port") is not None else 9023),
        "client(s)": "client" if n == 1 else "clients",
    }
    for key, value in values.items():
        text = text.replace(key, value)
    return text


def display(machine_state: str, ctx: dict) -> tuple:
    """(display, severity, message, hint) for a machine state (§4.6) and a context dict with any of:
    why (run_decision), fault (FAULT kind), data_role ("device" = charging/PC mode), last_failure
    (Failure value of the last failure, or "dark_start", until the next healthy frame), next_rung,
    overlay (STREAMING: "test_pattern" | "dark" | "brownout" while active; SETTLING/BACKOFF:
    "brownout" only), fps, clients, hub,
    settle_left_s, ir_level, owner, port, url (without the token: it reaches /status) and
    pixels ("swar" appends the reduced-frame-rate hint)."""
    ctx = ctx or {}
    name = _display_name(str(machine_state).upper(), ctx)
    severity, message, hint = DISPLAY[name]
    message, hint = _fill(name, message, ctx), _fill(name, hint, ctx)
    if name == "streaming" and ctx.get("hub"):
        hint = HUB_HINT
    if ctx.get("pixels") == "swar":
        hint = hint + "; " + SWAR_HINT if hint else SWAR_HINT
    return name, severity, message, hint

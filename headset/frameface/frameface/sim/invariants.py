# SPDX-License-Identifier: MIT
"""The trace invariants of the driver matrix (spec §11.3 test_ff_driver_matrix): check_trace(kernel.ops) checks the 13
invariants every matrix case asserts on a FakeKernel op trace (sim.replay's ctrl_name names its control requests)."""
from .. import uvc

__all__ = ["check_trace", "ACTIVATION", "TEARDOWN", "RECOVERY"]


ACTIVATION = ("MAGIC", "STREAM_OFF", "MAGIC") + ("REG_WRITE",) * 11 + ("MAGIC", "STREAM_ON")
TEARDOWN = ("MAGIC", "STREAM_OFF", "MAGIC") + ("REG_WRITE",) * 11 + ("MAGIC",)
RECOVERY = ("REG_WRITE",) * 11 + ("MAGIC",)     # Block C's prefix after a Block A that ended after MAGIC #2
_XU_SET = ("MAGIC", "STREAM_OFF", "STREAM_ON", "REG_WRITE", "REG_READ", "XU_SET")
_EP0 = ("ctrl", "setif", "setconfig", "clear_halt")     # the EP0 requests of a FakeKernel trace


def _ep0_silent(ops):
    """vft._ep0 over a trace: EP0 is silent after an EP0 request that ended with ETIMEDOUT, until one completes or
    STALLs (EPIPE); any other error leaves it as it was."""
    silent = False
    for op in ops:
        if op["kind"] in _EP0:
            err = op.get("err")
            silent = err == "ETIMEDOUT" or (silent and err not in (None, "EPIPE"))
    return silent


def _sessions(ops):
    sessions, order, current = {}, [], None
    for op in ops:
        kind, fd = op["kind"], op.get("fd")
        if kind == "open" and fd is not None:
            current = fd
            sessions[fd] = [op]
            order.append(fd)
        elif kind == "note":
            if current in sessions:
                sessions[current].append(op)
        elif fd in sessions:
            sessions[fd].append(op)
            if kind == "close" and fd == current:
                current = None
    return [(fd, sessions[fd]) for fd in order]


def _acked(s, i):
    """Did the XU SET_CUR at s[i] get a 0x56 answer before the next SET_CUR?"""
    for op in s[i + 1:]:
        if op["kind"] == "ctrl" and op["name"] in _XU_SET:
            return False
        if op["kind"] == "ctrl" and op["name"] == "XU_GET_CUR" and (op.get("in") or "").startswith("56"):
            return True
    return False


def _match(s, sets, start, seq, on):
    """How many of sets[start:] follow `seq`: REG_WRITEs in ir_regs order with PWM 0x02-0x04 non-zero iff
    `on`; a block ends after its first unacknowledged command (STREAM_OFF/STREAM_ON are tolerated, and an
    activation's failed MAGIC #3 is still followed by its STREAM_ON)."""
    k = 0
    while start + k < len(sets) and k < len(seq) and sets[start + k][0] == seq[k]:
        name, reg, val, i = sets[start + k]
        if name == "REG_WRITE" and (reg != uvc.IR_REG_ADDRS[k - seq.index(name)] or
                                    (reg in (2, 3, 4) and bool(val) != on)):
            break
        k += 1
        if name not in ("STREAM_OFF", "STREAM_ON") and not (seq is ACTIVATION and k == len(seq) - 1) and \
                not _acked(s, i):
            break
    return k


def _split(s, sets, blockless=False):
    """(activation, restart, recovery prefix, teardown, rest) counts of a session's XU SET_CURs. An activation that
    ended after MAGIC #1 (and STREAM_OFF) is followed by one STREAM_ON (restart); one that ended after MAGIC #2 and
    before MAGIC #3 by the prefix (11 OFF writes, MAGIC). The activation match is greedy, but Block C's commands can
    pass for an activation's (both start MAGIC, STREAM_OFF, MAGIC), so shorter activations are tried too: the first
    whole split wins (nothing left over, and Block C complete or ended at an unacknowledged command), else the one
    with the least rest. An activation with no Block C after it is whole only in a lost or crashed session
    (`blockless`): otherwise Block C ran, and a lone unacknowledged MAGIC is Block C's (a stop before MAGIC #1, then
    EP0 silent)."""
    best = None
    for act in range(_match(s, sets, 0, ACTIVATION, True), -1, -1):
        rst = int(act in (1, 2) and act < len(sets) and sets[act][0] == "STREAM_ON")
        rec = _match(s, sets, act + rst, RECOVERY, False) if 3 <= act < len(ACTIVATION) - 1 else 0
        td = _match(s, sets, act + rst + rec, TEARDOWN, False)
        end, rest = act + rst + rec + td, len(sets) - act - rst - rec - td
        bare = end == act + rst                         # no Block C after the activation
        whole = not rest and ((bare and (blockless or not act)) or td >= len(TEARDOWN) - 1 or
                              (not bare and not _acked(s, sets[end - 1][3])))
        if best is None or (not whole, rest) < best[0]:
            best = ((not whole, rest), (act, rst, rec, td, rest))
        if whole:
            break
    return best[1]


def _cadence(s, sets, start, end):
    """The §7.2 cadence (MAGICs counted since open or STREAM_ON; REG_WRITEs after MAGIC #2 buffered and latched at
    MAGIC #3) over sets[:end]: ((the MAGIC count where Block C (sets[start:end]) begins at the device, and as sent),
    None without one; a Block C MAGIC latched IR-on writes; latched PWM). "As sent" counts every MAGIC and STREAM_ON
    the driver sent, whatever came of it: the alignment the driver itself keeps (xublocks `_phase`)."""
    count, sent, pending, pwm, on_latch, first = 0, 0, [], {}, False, None
    for k, (name, reg, val, i) in enumerate(sets[:end]):
        if k == start:
            first = (count, sent)
        sent = 0 if name == "STREAM_ON" else sent + (name == "MAGIC")
        if s[i].get("err"):
            continue                                    # refused by the device: not part of the cadence
        if name == "STREAM_ON":
            count, pending = 0, []
        elif name == "REG_WRITE" and count == 2:
            pending.append((reg, val))
        elif name == "MAGIC":
            count += 1
            if count == 3 and pending:
                pwm.update((a, v) for a, v in pending if a in (2, 3, 4))
                pending, on_latch = [], on_latch or (k >= start and any(pwm.values()))
    return first, on_latch, pwm


def check_trace(ops, service: bool = True) -> list:
    """Problems with the 13 invariants in a FakeKernel op trace ([] = all hold). A runner adds one
    kernel.note(strategy=..., n=..., set_config=...) per activation session; note(abort=True) marks a
    stop that abandoned an activation, note(stop=True) a stop request (Service.request_stop() notes it after setting
    the drivers' abort), note(crash=True) a session closed without Block C on purpose.
    (9) includes Block C's cadence under §7.2: Block C starts at MAGIC count 0 (2 with the recovery prefix) as sent
    (every MAGIC and STREAM_ON the driver sent counts, so a missing STREAM_ON after MAGIC #1 or #3 is reported) and at
    the device (not checked when Block C's first command never reached it), no Block C MAGIC may latch buffered IR-on
    writes, and after a teardown whose trailing MAGIC (7b) reached the device no PWM register latched in the session
    stays on; and its tail: CLEAR_HALT, RELEASE 1, RELEASE 0, close, without the CLEAR_HALT exactly when EP0 is silent
    (an EP0 request ended with ETIMEDOUT and none answered since, vft._ep0) or a stop was noted before Block C (a stop
    noted during Block C may have come before or after step 11: either is accepted)."""
    problems = []
    for fd, s in _sessions(ops):
        def bad(k, msg):
            problems.append("fd %d (%d) %s" % (fd, k, msg))
        note, stop_i = {}, None
        for i, op in enumerate(s):
            if op["kind"] == "note":
                note.update({k: v for k, v in op.items() if k not in ("t", "kind", "fd")})
                if stop_i is None and (op.get("stop") or op.get("abort")):
                    stop_i = i                          # the first stop request noted in this session
        idx = {name: [i for i, op in enumerate(s) if op["kind"] == "ctrl" and op["name"] == name]
               for name in ("XU_GET_LEN", "COMMIT_SET", "STREAM_ON", "PROBE_SET")}
        claimed, first_claim, enodev = set(), None, None
        sets = []
        for i, op in enumerate(s):
            kind = op["kind"]
            if kind in ("claim", "disconnect_claim"):
                first_claim = i if first_claim is None else first_claim
                if "err" not in op:
                    claimed.add(op["iface"])
                if kind == "disconnect_claim" and (op["flags"] != 2 or op["driver"] != "usbfs"):
                    bad(11, "DISCONNECT_CLAIM flags 0x%x driver %r" % (op["flags"], op["driver"]))
            elif kind == "setconfig" and (not note.get("set_config") or first_claim is not None):
                bad(13, "SETCONFIGURATION outside an S5 open or after a CLAIM")
            elif kind == "setif" and op["inflight"]:
                bad(8, "SETINTERFACE with %d URBs in flight" % op["inflight"])
            elif kind == "reset" and (op["inflight"] or "." in op["sysname"]):
                bad(12, "RESET while streaming or behind a hub")
            elif kind == "ctrl":
                name = op["name"]
                if op["rt"] & 0x60 == 0x20 and not {0, 1} <= claimed:
                    bad(1, "%s before CLAIM 0 and 1" % name)
                if name in _XU_SET or name == "XU_GET_CUR":
                    if enodev is not None:
                        bad(10, "%s after ENODEV" % name)
                    if "n" in note and op["wl"] != note["n"]:
                        bad(5, "%s wLength %d != n %d" % (name, op["wl"], note["n"]))
                if name in _XU_SET:
                    if not idx["XU_GET_LEN"] or idx["XU_GET_LEN"][0] > i:
                        bad(2, "XU SET_CUR before GET_LEN")
                    if name == "REG_READ":
                        if service:
                            bad(4, "REG_READ in a service run")
                        continue                        # probe readbacks (A2, A12) are not part of a block
                    sets.append((name, op.get("reg"), op.get("val"), i))
            if op.get("err") == "ENODEV" or kind == "enodev" or (kind == "reap" and op.get("st") == -19):
                enodev = i if enodev is None else enodev        # -108 (ESHUTDOWN) is a flush, not a loss
        lost = enodev is not None
        act, rst, rec, td, rest = _split(s, sets, lost or bool(note.get("crash")))
        start = act + rst
        end = start + rec + td                          # sets[start:end]: Block C's XU part
        committed = bool(idx["COMMIT_SET"])
        if rest:
            bad(3, "unexpected XU commands %s" % [x[0] for x in sets[end:]])
        if committed and act != len(ACTIVATION):
            bad(3, "COMMIT after an incomplete activation (%d commands)" % act)
        if 0 < act < len(ACTIVATION) and not (note.get("abort") or lost or not _acked(s, sets[act - 1][3])):
            bad(3, "activation stopped after an acknowledged %s" % sets[act - 1][0])
        if act and not note.get("crash") and not lost and end == start:
            bad(9, "no teardown XU part after an activation")
        if start < end and (0 < rec < len(RECOVERY) or td < len(TEARDOWN) - 1) and not lost and \
                _acked(s, sets[end - 1][3]) and sets[end - 1][0] != "STREAM_OFF":
            bad(9, "teardown XU part stopped after an acknowledged %s" % sets[end - 1][0])
        first, on_latch, pwm = _cadence(s, sets, start, end)
        want = 2 if rec else 0
        # as sent: the driver's own alignment; at the device: a command that never reached it (err) does not count,
        # which matters only if Block C's first command reached it (a STALL or a silent EP0 ended Block C before)
        if first is not None and first[1] != want:
            bad(9, "Block C's MAGIC cadence misaligned: Block C starts at MAGIC count %d" % first[1])
        elif first is not None and first[0] != want and not s[sets[start][3]].get("err"):
            bad(9, "Block C's MAGIC cadence misaligned: Block C starts at MAGIC count %d" % first[0])
        if on_latch or (td == len(TEARDOWN) and not s[sets[end - 1][3]].get("err") and any(pwm.values())):
            bad(9, "Block C's MAGIC cadence misaligned: %s" % ("a Block C MAGIC latched IR-on writes" if on_latch
                                                               else "IR on after the trailing MAGIC"))
        if note.get("strategy") != "S4" and act:
            first, last = sets[0][3], sets[act - 1][3]
            if any(first < j < last for j in idx["XU_GET_LEN"]):
                bad(3, "GET_LEN inside a non-S4 activation")
        if committed and idx["STREAM_ON"] and idx["COMMIT_SET"][-1] < idx["STREAM_ON"][-1]:
            bad(6, "COMMIT before STREAM_ON")
        submits = [i for i, op in enumerate(s) if op["kind"] == "submit"]
        if committed and submits:
            ref = s[idx["COMMIT_SET"][-1]]["t"]
            for op in s[idx["COMMIT_SET"][-1]:submits[0]]:
                if op["kind"] == "setif":
                    ref = op["t"]
            if s[submits[0]]["t"] - ref > 0.05:
                bad(7, "first SUBMITURB %.3f s after COMMIT" % (s[submits[0]]["t"] - ref))
        close = s[-1] if s[-1]["kind"] == "close" else None
        if (act or end > start) and not lost and not note.get("crash"):
            tail = [(i, op) for i, op in enumerate(s) if i > sets[end - 1][3] and op["kind"] in (
                "discard", "clear_halt", "release", "close", "ctrl", "submit")]
            if any(op["kind"] == "ctrl" and op["name"] in _XU_SET and op["name"] != "REG_READ" for _, op in tail):
                bad(9, "XU command after the teardown XU part")
            seq = [(i, op["kind"] if op["kind"] != "release" else "release%d" % op["iface"]) for i, op in tail
                   if op["kind"] in ("clear_halt", "release", "close")]
            names = [k for _, k in seq]
            # step 11 CLEAR_HALT unless EP0 is silent (vft._ep0: an ETIMEDOUT, nothing answered since) or the driver
            # saw a stop (vft._halt_kept: noted before Block C), then 12, 13; a stop noted later: either
            c0 = sets[start][3] if end > start else (seq[0][0] if seq else len(s))
            stopping = stop_i is not None and stop_i < c0
            if names[-3:] != ["release1", "release0", "close"]:
                bad(9, "teardown tail %s" % names[-4:])
            elif names[-4:-3] == ["clear_halt"]:
                if _ep0_silent(s[:seq[-4][0]]):
                    bad(9, "CLEAR_HALT to a silent EP0 (an ETIMEDOUT and nothing answered since)")
                elif stopping:
                    bad(9, "CLEAR_HALT with a stop pending")
            elif not (_ep0_silent(s[:seq[-3][0]]) or (stop_i is not None and stop_i < seq[-3][0])):
                bad(9, "teardown tail %s" % names[-4:])
            data = next((i for i, op in enumerate(s) if op["kind"] == "reap" and op.get("len")), None)
            last = sets[end - 1][3] if end > start else None    # discards at start (URB size halving) are allowed
            if data is not None and last is not None and any(
                    data < i < last for i, op in enumerate(s) if op["kind"] == "discard"):
                bad(9, "DISCARDURB before the teardown XU part ended")
        if close is not None and not note.get("crash") and close["inflight"] + close["unreaped"]:
            bad(9, "%d URBs in flight at close" % (close["inflight"] + close["unreaped"]))
    return problems

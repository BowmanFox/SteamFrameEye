# SPDX-License-Identifier: MIT
"""The /status document of the service (spec §4.18): schema v1, every key always present (null when
unknown), never the token or pixels; and the context of the display table (policy.display)."""
import collections
import json
import os
import platform

from . import __version__
from .descriptors import sha1_hex
from .policy import IR_LEVELS, display

__all__ = ["build_status", "display_ctx", "url_hint"]


def _r(v, nd=1):
    return None if v is None else round(v, nd)


def url_hint(svc, port):
    """The camera address for the waiting_client hint, without the token (the hint reaches /status)."""
    path = "/stream/<token>" if svc.cfg.token else "/stream"
    return "http://10.35.78.1:%d%s (python3 -I ~/frameface/frameface_main.py url prints it)" % (port, path)


def display_ctx(svc, now, nclients):
    """The context of policy.display(): overlays while streaming (brown-out for 60 s after the IR
    level was lowered, dark after 2 s of dark frames, test_pattern after 10 test-bar frames)."""
    a, node = svc.act, svc.node
    overlay = ""
    if now < svc.overlay_until:
        overlay = svc.overlay
    elif a is not None and a.dark_since is not None and now - a.dark_since >= 2.0:
        overlay = "dark"
    elif a is not None and a.testbar_run >= 10:
        overlay = "test_pattern"
    fault = svc.fault["kind"] if svc.fault else None
    port = svc.http_port or svc.bind[1]
    owner = (svc.port_fault or {}).get("owner") if fault == "port" else svc.owner
    asm = a.asm if a is not None else None
    return {"why": svc.decision.why, "fault": fault, "data_role": svc.data_role, "last_failure": svc.last_failure,
            "next_rung": svc.next_rung, "overlay": overlay, "fps": asm.fps.fps_in if asm else 0,
            "clients": nclients, "hub": bool(node and node.behind_hub), "settle_left_s": svc.settle_left,
            "ir_level": svc._level(), "owner": owner, "port": port, "url": url_hint(svc, port),
            "pixels": svc.pre.name}


def _selftest_exit(svc):
    try:
        with open(svc.paths.selftest, encoding="utf-8") as f:
            v = json.load(f).get("exit")
        return v if isinstance(v, int) else None
    except (OSError, ValueError, AttributeError):
        return None


def build_status(svc) -> dict:
    now, bt = svc.clock.monotonic(), svc.clock.boottime()
    a, node, ident = svc.act, svc.node, svc.ident
    clients = svc.server.clients() if svc.server is not None and svc.http_port else []
    nclients = svc._clients()
    state = svc.state if svc.state != "STOPPED" else "TEARDOWN"
    disp, severity, message, hint = display(state, display_ctx(svc, now, nclients))
    asm = a.asm if a is not None else None
    reasm = a.reasm if a is not None else None
    stats = asm.stats if asm is not None else collections.Counter()
    drv = svc.driver or (svc.drivers[-1] if svc.drivers else None)
    usb = drv.stats if drv is not None else {}
    lay = ident.layout if ident is not None else None
    params = a.params if a is not None else None
    level = svc._level()
    prof = svc._last_profile
    rates = svc.server.rates() if svc.server is not None and svc.http_port else {"kbps": 0, "fps_out": 0.0}
    srv = svc.server.stats if svc.server is not None else collections.Counter()
    plane = svc.plane_info or {"verdict": None, "mad": None, "std_odd": None, "std_even": None, "h2_hint": None}
    ladder_, rung, tried = list(svc.pass_ladder or []), svc.rung, set(svc.tried)    # one consistent read
    return {
        "v": 1,
        "service": {"version": __version__, "uptime_s": _r(now - svc.start_t) if svc.start_t else 0.0,
                    "pid": os.getpid(), "python": platform.python_version(), "pixels": svc.pre.name,
                    "jpeg": svc._jpeg_name(), "selftest": _selftest_exit(svc)},
        "state": svc.state, "display": disp, "severity": severity, "message": message, "hint": hint,
        "since_s": _r(now - svc.state_t),
        "policy": {"enabled": bool(svc.cfg.enabled and svc.flag_absent), "power_policy": svc.cfg.power_policy,
                   "worn": svc.worn, "clients": nclients, "publish": svc.publish, "why": svc.decision.why},
        "device": {"present": svc.present, "node": node.devpath if node else None,
                   "sysname": node.sysname if node else None, "speed": node.speed_mbps if node else None,
                   "hub": node.behind_hub if node else None,
                   "writable": os.access(node.devpath, os.W_OK) if node else None,
                   "get_len": ident.get_len if ident else None, "xu_unit": ident.xu_unit if ident else None,
                   "ep": lay.vs_endpoint if lay else None, "max_packet": lay.max_packet if lay else None,
                   "bcd_uvc": "0x%04x" % lay.bcd_uvc if lay else None,
                   "desc_sha1": sha1_hex(svc.desc) if svc.desc else None, "data_role": svc.data_role,
                   "pm": {"control": svc.pm.get("control"), "runtime_status": svc.pm.get("runtime_status")}},
        "negotiated": {"probe_hex": params.probe_hex if params else None,
                       "probe_len": params.probe_len if params else None,
                       "max_frame": params.max_frame if params else None,
                       "max_payload": params.max_payload if params else None,
                       "interval": params.interval if params else None,
                       "framing_info": params.framing_info if params else None},
        "strategy": {"name": rung, "source": svc.rung_source,
                     "ladder_pos": ladder_.index(rung) if rung in ladder_ else None,
                     "tried": [r for r in ladder_ if r in tried], "xu_len": a.n if a else None,
                     "set_config": ident.set_config if ident else None},
        "ir": {"level": level, "pwm": svc.cfg.ir_pwm,
               "current": svc.cfg.ir_current if svc.cfg.ir_current is not None else IR_LEVELS[level],
               "reason": prof.ir_reason if prof else "", "trial": svc.trial},
        "stream": {"fps_in": _r(asm.fps.fps_in) if asm else None, "fps_pts": _r(asm.fps.fps_pts) if asm else None,
                   "fps_out": rates["fps_out"],     # a torn-down activation is already in frames_done
                   "frames": svc.frames_done + (stats["frames_ok"] if asm is not None and not a.counted else 0),
                   "published": svc.published,
                   "bad": {"size": stats["bad_size"], "err": stats["bad_err"], "hdr": stats["bad_hdr"],
                           "overflow": stats["overflow"], "fid_breaks": stats["fid_breaks"],
                           "size_614400": stats["unexpected_size_614400"], "dropped_busy": stats["dropped_busy"]},
                   "testbar": svc.counts["testbar"], "dark": svc.counts["dark"], "frozen": svc.counts["frozen"],
                   "y_phase": None if svc.phase is None else ("odd" if svc.phase else "even"), "plane": plane,
                   "assembler": {"mode": asm.mode if asm else None,
                                 "stride": reasm.max_payload if reasm else None,
                                 "stride_learned": bool(stats["stride_learned"])},
                   "kbps": rates["kbps"], "jpeg_kb": _r(svc.jpeg_kb), "quality": svc.cfg.jpeg_quality,
                   "pre_ms": _r(svc.pre_ms, 2), "jpeg_ms": _r(svc.jpeg_ms, 2), "jpeg_bad": svc.jpeg_bad,
                   "jpeg_warn": getattr(svc.encoder, "stats", {}).get("jpeg_warn", 0) if svc.encoder else 0,
                   "placeholders": srv["placeholders"], "keepalives": srv["keepalives"]},
        "usb": {"urbs": usb.get("urbs"), "urb_bytes": usb.get("urb_bytes"), "inflight_min": usb.get("inflight_min"),
                "reap_batch_max": usb.get("reap_batch_max"),
                "errors": {k: usb.get(k, 0) for k in ("epipe", "eproto", "eilseq", "etime", "eoverflow", "other")}},
        "recovery": {"activations": svc.counts["activations"], "soft_reopens": svc.counts["soft_reopens"],
                     "resets": svc.counts["resets"], "unplugs": svc.counts["unplugs"],
                     "brownout_suspects": svc.brown.total, "consecutive_failures": svc.fails_in_row,
                     "backoff_s": _r(max(0.0, svc.backoff_until - bt)) if svc.state == "BACKOFF" else 0,
                     "settle_left_s": _r(svc.settle_left), "dirty_start": svc.dirty_start},
        "power": {"battery_pct": svc.battery.get("pct"), "charging": svc.battery.get("charging")},
        "clients": clients,
        "events": svc.log.recent(20) if hasattr(svc.log, "recent") else [],
    }

# SPDX-License-Identifier: MIT
"""The install gate (spec §6): `frameface_main.py selftest [--quick] [--lib DIR] [--json] [--result FILE]`.

1. ABI: <lib>/abi_dump (or $FRAMEFACE_ABI_DUMP without --lib) prints the usbfs ABI as JSON; it must
   equal usbfs.abi_table() key by key (a mismatch exits 2; a missing binary is "skip").
2. Pixels: make_preprocessor(lib, "c"): the C helper must pass the parity gate; a helper that exists
   but fails it (load, ABI or parity) exits 3 at the end; no helper at all is plain SWAR.
3. JPEG: TurboJPEG must encode preprocess_swar(lcg_frame(1)) into a valid JPEG (and decode it when a
   decoder exists); a missing library exits 4 and step 4 uses StubJpegEncoder.
4. Simulation (not with --quick): FakeKernel + FakeVft at 60 fps on the real clock behind the threaded
   Service with HTTP on 127.0.0.1:0, all on a temporary tree (temp_paths, make_rig, worn unknown, its
   own events.jsonl, never the real flock); a BabaClient must read >= 10 parts within 5 s
   (FRAMEFACE_NO_FPS_ASSERT=1: >= 3 within 30 s); after service.stop() the fake must have 0 URBs in
   flight, no violations and the 13 trace invariants (incl. the teardown XU part before DISCARDURB).

Exit: 2 if step 1 or 4 failed, else 3 if parity failed, else 4 if TurboJPEG is missing, else 0. The
result JSON (version, exit, abi, pixels, jpeg, sim, when) goes to `result_path` (default the state
dir's selftest.json). FRAMEFACE_SELFTEST_FORCE=<code> writes a result with that exit code and
returns it without running anything (tests of install.sh only).
"""
import datetime
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

from . import __version__
from .clock import RealClock
from .config import Config, atomic_write, default_paths, ensure_dir, new_token, temp_paths
from .jpeg import JpegEncoder, JpegError, StubJpegEncoder, decode_gray, validate_jpeg
from .logutil import Log
from .preprocess import SwarPreprocessor, lcg_frame, make_preprocessor, preprocess_swar
from .usbfs import abi_table

__all__ = ["run_selftest", "compare_abi", "write_result", "FORCE_ENV"]

FORCE_ENV = "FRAMEFACE_SELFTEST_FORCE"


def _when():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compare_abi(dump: dict, table: dict) -> list:
    """Differences between abi_dump's JSON and abi_table(), key by key ([] = equal)."""
    out = []
    for section in sorted(set(dump) | set(table)):
        a, b = dump.get(section), table.get(section)
        if not isinstance(a, dict) or not isinstance(b, dict):
            if a != b:
                out.append("%s: %r != %r" % (section, a, b))
            continue
        for key in sorted(set(a) | set(b)):
            if a.get(key) != b.get(key):
                out.append("%s.%s: abi_dump %r != python %r" % (section, key, a.get(key), b.get(key)))
    return out


def write_result(path: str, result: dict) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(folder):               # never chmod an existing folder (~/frameface.new stays 0755)
        ensure_dir(folder)
    atomic_write(path, (json.dumps(result, indent=1) + "\n").encode("utf-8"), 0o600)


def _abi_dump_path(lib_dir):
    if lib_dir:
        return os.path.join(lib_dir, "abi_dump")
    env = os.environ.get("FRAMEFACE_ABI_DUMP", "").strip()
    return env or os.path.join(os.path.expanduser("~"), "frameface", "lib", "abi_dump")


def _run_abi_dump(path):
    """The JSON printed by the abi_dump binary at `path`; None if it does not exist."""
    if not os.path.isfile(path):
        return None
    proc = subprocess.run([path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False)
    if proc.returncode != 0:
        raise ValueError("abi_dump exited %d" % proc.returncode)
    return json.loads(proc.stdout.decode("utf-8"))


def _helper_failed(pre):
    """A helper that exists but was rejected (load, ABI or parity), as opposed to no helper at all."""
    if not isinstance(pre, SwarPreprocessor) or not pre.reason:
        return False
    return pre.reason in ("abi", "parity") or bool(pre.path and os.path.exists(pre.path))


def run_selftest(lib_dir: "str | None", quick: bool = False, encoder=None, out=print, result_path: "str | None" = None,
                 *, sim_knobs: "dict | None" = None) -> int:
    out = out or (lambda *a: None)
    result_path = result_path or default_paths().selftest
    forced = os.environ.get(FORCE_ENV, "").strip()
    if forced:
        code = int(forced)
        write_result(result_path, {"v": 1, "version": __version__, "exit": code, "abi": "skip", "pixels": "swar",
                                   "jpeg": "missing", "sim": "skip", "when": _when(), "forced": True})
        out("[warn] selftest forced to exit %d (%s)" % (code, FORCE_ENV))
        return code
    res = {"v": 1, "version": __version__, "exit": None, "abi": "skip", "pixels": "swar", "jpeg": "missing",
           "sim": "skip", "when": _when()}
    # 1 ABI
    path = _abi_dump_path(lib_dir)
    try:
        dump = _run_abi_dump(path)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        dump = e
    if dump is None:
        out("[warn] abi: %s not found (skip)" % path)
    elif isinstance(dump, Exception):
        res["abi"] = "fail"
        out("[fail] abi: %s: %s" % (path, dump))
    else:
        diff = compare_abi(dump, abi_table())
        res["abi"] = "fail" if diff else "ok"
        for line in diff[:20]:
            out("[fail] abi: " + line)
        if not diff:
            out("[ok] abi: abi_dump == usbfs.abi_table()")
    # 2 pixels
    pre = make_preprocessor(lib_dir, "c")
    res["pixels"] = pre.name
    parity_failed = _helper_failed(pre)
    if parity_failed:
        out("[fail] pixels: C helper rejected (%s): %s; SWAR is used" % (pre.reason, pre.path))
    elif pre.name == "swar":
        out("[warn] pixels: no C helper (%s); SWAR at <= 30 fps" % (pre.path or "none"))
    else:
        out("[ok] pixels: C helper == SWAR on the parity set")
    # 3 JPEG
    enc = encoder
    if enc is None:
        try:
            enc = JpegEncoder.load()
        except JpegError as e:
            out("[fail] jpeg: %s" % e)
            enc = None
    if enc is not None:
        try:
            data = enc.encode_gray(preprocess_swar(lcg_frame(1)))
            ok = validate_jpeg(data)
            if ok and getattr(enc, "api", "") in ("tj3", "tj2"):
                px = decode_gray(data)
                ok = px is None or len(px) == 160000
        except (JpegError, ValueError) as e:
            out("[fail] jpeg: %s" % e)
            ok = False
        if ok:
            res["jpeg"] = getattr(enc, "api", "stub")
            out("[ok] jpeg: %s, %d bytes" % (res["jpeg"], len(data)))
        else:
            enc = None
    # 4 simulation
    if not quick:
        problems = _simulate(pre, enc if enc is not None else StubJpegEncoder(), sim_knobs or {})
        res["sim"] = "fail" if problems else "ok"
        for p in problems:
            out("[fail] sim: " + p)
        if not problems:
            out("[ok] sim: stream, teardown and trace invariants")
    if res["abi"] == "fail" or res["sim"] == "fail":
        code = 2
    elif parity_failed:
        code = 3
    elif res["jpeg"] == "missing":
        code = 4
    else:
        code = 0
    res["exit"] = code
    write_result(result_path, res)
    out("selftest exit %d" % code)
    return code


def _simulate(pre, enc, knobs) -> list:
    """Step 4 on an isolated temporary tree; the list of problems ([] = pass)."""
    from .service import Service
    from .sim import VftModel, check_trace, make_rig
    from .sim.babaclient import BabaClient
    from .worn import NO_WORN
    fps_asserts = os.environ.get("FRAMEFACE_NO_FPS_ASSERT", "").strip() != "1"
    need, limit = (10, 5.0) if fps_asserts else (3, 30.0)
    root = tempfile.mkdtemp(prefix="ffselftest-")
    rig = svc = client = thread = None
    problems = []
    try:
        paths = temp_paths(root)
        model = dict({"fps": 60.0}, **knobs)
        rig = make_rig(VftModel(**model), clock=RealClock(), tmp=os.path.join(root, "rig"))
        cfg = Config(token=new_token(), allow_peers=["127.0.0.0/8"], cpu_io="auto", cpu_work="auto", nice_io=0)
        log = Log("info", paths.events, journal=True, stream=io.StringIO(), clock=rig.clock)
        svc = Service(cfg, paths, sys=rig.kernel, clock=rig.clock, sys_root=rig.sys_root, dev_root=rig.dev_root,
                      proc_root=rig.proc_root, encoder=enc, pre=pre, worn=NO_WORN, threaded=True, http=True,
                      log=log, bind=("127.0.0.1", 0))
        svc.trace_note = rig.kernel.note
        svc.startup()
        if not svc.http_port:
            return ["HTTP did not bind on 127.0.0.1"]
        stop = threading.Event()
        thread = threading.Thread(target=svc.run, args=(stop,), name="selftest-service", daemon=True)
        thread.start()
        decode = decode_gray if getattr(enc, "api", "") in ("tj3", "tj2") else None
        client = BabaClient("http://127.0.0.1:%d/stream/%s" % (svc.http_port, cfg.token), decode=decode).start()
        if not client.wait_frames(need, limit):
            problems.append("%d of %d parts within %.0f s (state %s)" % (client.frames, need, limit, svc.state))
        client.stop()
        svc.stop()
        thread.join(10.0)
        k = rig.kernel
        if not any(op["kind"] == "ctrl" and op.get("name") == "COMMIT_SET" for op in k.ops):
            problems.append("the tracker was never started")
        if k.inflight():
            problems.append("%d URBs in flight after stop" % k.inflight())
        problems += ["violation " + v for v in k.violations]
        problems += ["trace " + p for p in check_trace(k.ops)]
        if svc.errors:
            problems.append("service error %r" % (svc.errors[0],))
    except Exception as e:                      # a broken simulation is a failed gate, never a crash
        problems.append("simulation crashed: %r" % (e,))
    finally:
        if client is not None:
            client.stop()
        if svc is not None:
            svc.stop()
        if thread is not None:
            thread.join(5.0)
        if rig is not None:
            rig.close()
        _rmtree(root)
    return problems


def _rmtree(root):
    for _ in range(5):
        shutil.rmtree(root, ignore_errors=True)
        if not os.path.exists(root):
            return
        time.sleep(0.1)

#!/usr/bin/env python3
"""Record raw Steam Frame eye-server samples to CSV for tuning (read-only access to the shared memory).

    python3 record_eyes.py <out.csv> [seconds]

One row per eye-server sample (90 Hz while the headset is worn): headset time, fused per-eye gaze,
gaze covariance, fixation point, pre-fusion gaze and covariance, eyelid openness and the eight
undocumented extra values. Replay it on the PC with tools/ReplayProbe to test the module's processing.
"""
import mmap, os, struct, sys, time
out, dur = sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 300.0
fd = os.open("/dev/shm/eye-server.mmap", os.O_RDONLY)
mm = mmap.mmap(fd, os.fstat(fd).st_size, access=mmap.ACCESS_READ)
FMT = "<IBd6f6f3f6f6f2f8f"
cols = ("state,flag,t,gLx,gLy,gLz,gRx,gRy,gRz,cLx,cLy,cLz,cRx,cRy,cRz,fx,fy,fz,"
        "pLx,pLy,pLz,pRx,pRy,pRz,pcLx,pcLy,pcLz,pcRx,pcRy,pcRz,oL,oR,e0,e1,e2,e3,e4,e5,e6,e7")
last, n, end = None, 0, time.monotonic() + dur
with open(out, "w") as fh:
    fh.write("mono,seq," + cols + "\n")
    while time.monotonic() < end:
        seq = struct.unpack_from("<I", mm, 0x38)[0]
        if seq != last:
            if last is not None:
                v = struct.unpack_from(FMT, mm, 0x152)
                fh.write("%.4f,%d,%d,%d,%s\n" % (time.monotonic(), seq, v[0], v[1], ",".join("%.6g" % x for x in v[2:])))
                n += 1
            last = seq
        else:
            time.sleep(0.002)
print("recorded", n)

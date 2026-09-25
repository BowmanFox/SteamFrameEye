"""Send synthetic SFEY packets to the local VRCFT module (loopback bypasses the firewall)."""
import math, socket, struct, sys, time

host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 9021
count = int(sys.argv[3]) if len(sys.argv) > 3 else 60
fmt = "<4sHHId3f3f3f2f3f3fd"
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
t0 = time.monotonic()
for i in range(count):
    yaw = math.radians(15.0 * math.sin(i / 10.0))          # sweep +-15 degrees
    gx, gz = math.sin(yaw), -math.cos(yaw)
    blink = 0.1 if (i % 30) < 3 else 0.9
    pkt = struct.pack(fmt, b"SFEY", 1, 1, 1000 + i, time.monotonic() - t0,
                      gx, 0.0, gz, gx, 0.0, gz, 10 * gx, 0.0, 10 * gz,
                      blink, blink, 0.006, 0.006, 0.006, 0.006, 0.006, 0.006, time.monotonic())
    s.sendto(pkt, (host, port))
    time.sleep(1 / 60)
print("sent", count, "packets to", host, port)

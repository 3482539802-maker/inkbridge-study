"""Send clearly labelled synthetic pen trajectories to the PC receiver; no tablet needed."""
import argparse
import json
import math
import socket
import time


def demo_points():
    strokes = [
        [(350 + 450*t, 450 + 160*math.sin(t*math.pi*2)) for t in [i/120 for i in range(121)]],
        [(950 + 380*t, 450 + 150*math.sin(t*math.pi*3)) for t in [i/120 for i in range(121)]],
        [(500, 1050), (650, 900), (650, 1450)],
        [(950+170*math.cos(a), 1020-130*math.sin(a)) for a in [math.pi-i*math.pi/60 for i in range(61)]]
        + [(1120-340*t, 1020+360*t) for t in [i/70 for i in range(1,71)]] + [(1120,1380)],
        [(1460+170*math.cos(a), 1010-120*math.sin(a)) for a in [math.pi-i*math.pi/60 for i in range(61)]]
        + [(1460,1140)]
        + [(1460+170*math.cos(a), 1260-120*math.sin(a)) for a in [math.pi-i*math.pi/60 for i in range(61)]],
    ]
    seq = 0
    clock = 1000000
    for stroke in strokes:
        down = clock
        for index, (x,y) in enumerate(stroke):
            seq += 1
            action = 1 if index == 0 else (3 if index == len(stroke)-1 else 2)
            yield {"type":"point", "seq":seq, "action":action, "pointer_id":0,
                   "x":round(x), "y":round(y), "action_time":clock, "down_time":down,
                   "pressure":0.45 + 0.2*math.sin(index/12), "tool_type":1,
                   "window_id":1, "display_id":0, "data_source":"synthetic-demo"}
            clock += 4000
        clock += 200000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--delay", type=float, default=0.004)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 0 <= args.delay <= 1:
        parser.error("Invalid port or delay")
    with socket.create_connection(("127.0.0.1", args.port), timeout=3) as connection:
        count = 0
        for point in demo_points():
            connection.sendall((json.dumps(point)+"\n").encode())
            count += 1
            if args.delay:
                time.sleep(args.delay)
    print(f"Sent {count} synthetic points. These are simulated curves and digits, not tablet handwriting.")


if __name__ == "__main__":
    main()

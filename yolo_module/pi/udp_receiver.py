#!/usr/bin/env python3
"""
아이폰(또는 다른 Pi)이 보낸 경고 JSON 받아서 출력 + 부저
  python3 udp_receiver.py --port 5005 --buzzer-pin 17
"""
import argparse
import json
import socket
import time

NAME = {0: "CLEAR", 1: "DETECTED", 2: "WARNING"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--buzzer-pin", type=int, default=None)
    args = ap.parse_args()

    buzzer = None
    if args.buzzer_pin is not None:
        from gpiozero import Buzzer
        buzzer = Buzzer(args.buzzer_pin)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    print(f"listening on :{args.port}")
    level = 0
    try:
        while True:
            data, addr = sock.recvfrom(65535)
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            new = int(msg.get("level", 0))
            ids = [d.get("id") for d in msg.get("drones", msg.get("tracks", []))]
            if new != level:
                print(f"[{time.strftime('%H:%M:%S')}] {addr[0]}: {NAME[level]} -> {NAME[new]} ids={ids}")
                if buzzer:
                    buzzer.off()
                    if new == 2:
                        buzzer.beep(on_time=0.1, off_time=0.1)
                    elif new == 1:
                        buzzer.beep(on_time=0.1, off_time=0.9)
                level = new
    except KeyboardInterrupt:
        pass
    finally:
        if buzzer:
            buzzer.off()


if __name__ == "__main__":
    main()

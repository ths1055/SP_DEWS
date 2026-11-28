"""fake_lora.py — 하드웨어 없는 시험용 가짜 E22 (test_alert.py, test_link.py 공용)"""
import queue


class FakeLora:
    """send()한 바이트가 상대의 recv()로 나옴. drop > 0이면 그만큼 송신을 버림 (무선 유실 흉내)."""

    def __init__(self):
        self.inbox: queue.Queue = queue.Queue()
        self.peer = None
        self.drop = 0

    def send(self, data: bytes) -> None:
        if self.drop:
            self.drop -= 1
            return
        self.peer.inbox.put(data)

    def recv(self) -> bytes:
        try:
            return self.inbox.get(timeout=0.005)
        except queue.Empty:
            return b""


def pair() -> tuple[FakeLora, FakeLora]:
    """서로 연결된 가짜 모듈 2개 (관측소, 지휘소)."""
    a, b = FakeLora(), FakeLora()
    a.peer, b.peer = b, a
    return a, b

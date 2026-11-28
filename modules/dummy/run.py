import threading
import time
from lcd import LCD
from e22 import E22
from protocol import Alert, SeqCounter, encode_alert, now_ms16
from queue import Queue
from dataclasses import dataclass

NODE_ID = 0

#pipeline에 들어갈 Event 형식
@dataclass
class Event:
    id: int #인식 개체 id? seq?
    status:str #거리 상태
    dist:float
    vel:float #(가능하다면)속도

class Pipeline:
    def __init__(self):
        self._msg_q = Queue()
        self._display_q = Queue()
        self._lcd = LCD()
        self._lora = E22("/dev/serial0",True)
        self._seq = SeqCounter()
        self._runningFlag = True

    def _to_alert(self, e: Event) -> Alert:
        seq = self._seq.next()
        track_id = e.id
        level = e.status
        stereo = True
        dist = e.dist
        speed = e.vel
        return Alert(NODE_ID, seq, track_id, level, stereo, dist, speed)

    def thread_lcd(self):
        while self._runningFlag:
            if not self._display_q.empty():
                data:Event = self._display_q.get()
                string = [[f'ID:{data.id}|sta:{data.status}'],
                          [f'VEL:{data.vel}']]
                self._lcd.write(string)
            time.sleep(0.01)

    def thread_transceiver(self):
        while self._runningFlag:
            if not self._msg_q.empty():
                data:Event = self._msg_q.get()
                try:
                    self._lora(encode_alert(self._to_alert(data)))
                    self._lora.wait_aux(2.0)
                except (TimeoutError, ValueError) as e:
                    print(f"LoRa:SEND FAIL {e}")
    
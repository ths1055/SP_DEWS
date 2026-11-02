import serial, time
import queue
from gpiozero import DigitalOutputDevice, DigitalInputDevice

m0 = DigitalOutputDevice(22)   # 핀 번호는 배선에 맞게 변경
m1 = DigitalOutputDevice(27)
aux = DigitalInputDevice(17)


class lora:
    def __init__(self, queue:queue.Queue):
        self.ser = serial.Serial("/dev/serial0", 9600, timeout=0.05) #50ms
        self.m0 = DigitalOutputDevice(22)   # 핀 번호는 배선에 맞게 변경
        self.m1 = DigitalOutputDevice(27)
        self.aux = DigitalInputDevice(17)
        self.queue = queue

    def set_mode(mode):            # 0: 일반, 2: 설정
        m0.value, m1.value = {0: (0, 0), 1: (1, 0), 2: (0, 1), 3: (1, 1)}[mode]
        time.sleep(0.05)
        while not aux.value:       # AUX가 HIGH가 되면 준비 완료
            time.sleep(0.01)

    def thread_polling(self):
        while self.queue.empty():
            data = self.queue.get()

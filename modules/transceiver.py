import serial, time
import queue
from data import data
from data import DataConvert
from gpiozero import DigitalOutputDevice, DigitalInputDevice

m0 = DigitalOutputDevice(22)   # 핀 번호는 배선에 맞게 변경
m1 = DigitalOutputDevice(27)
aux = DigitalInputDevice(17)


class lora:
    def __init__(self, id:int):
        self.ser = serial.Serial("/dev/serial0", 9600, timeout=0.05) #50ms
        self.m0 = DigitalOutputDevice(22)   # 핀 번호는 배선에 맞게 변경
        self.m1 = DigitalOutputDevice(27)
        self.aux = DigitalInputDevice(17)
        self.converter = DataConvert
        self._id=id

    def set_mode(mode):            # 0: 일반, 2: 설정
        m0.value, m1.value = {0: (0, 0), 1: (1, 0), 2: (0, 1), 3: (1, 1)}[mode]
        time.sleep(0.05)
        while not aux.value:       # AUX가 HIGH가 되면 준비 완료
            time.sleep(0.01)

    def send_message(self, dist:str) -> None: #msg queue 가져오는 로직 설계 필요
        to_send_data = data(0, self._id, dist)
        to_send_data_bin = self.converter.data_to_binary(to_send_data)
        self.ser.write(to_send_data_bin)
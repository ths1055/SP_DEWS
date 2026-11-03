from dataclasses import dataclass
import struct

@dataclass
class data:
    type:int
    node:int
    dist:float
    crc:int

class DataConvert:
    @staticmethod
    def data_to_binary(frame:data) -> bytes:
        binary_data: bytes = struct.pack('!iifI', frame.type, frame.node, frame.dist, frame.crc)
        return binary_data

    @staticmethod
    def binary_to_data(recv_bytes:bytes) -> data:
        recv_data = data()
        recv_data.type, recv_data.node, recv_data.dist, recv_data.crc = struct.unpack('!iifI', recv_bytes)
        return recv_data

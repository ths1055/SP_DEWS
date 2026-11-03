from dataclasses import dataclass
import struct
import zlib

@dataclass
class data:
    type:int #type 0:data frame, 1:ack
    node:int
    dist:float

class DataConvert:
    @staticmethod
    def data_to_binary(frame:data) -> bytes:
        binary_data: bytes = struct.pack('!iif', frame.type, frame.node, frame.dist)
        binary_data += struct.pack('!I', zlib.crc32(binary_data))
        return binary_data

    @staticmethod
    def binary_to_data(recv_bytes:bytes) -> tuple[data, int]:
        crc_res = zlib.crc32(recv_bytes)
        if crc_res != 0:
            return None, -1
        else:
            recv_type, node, dist = struct.unpack('!iif', recv_bytes)
            recv_data = data(recv_type, node, dist)
            return recv_data, crc_res

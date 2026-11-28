from RPLCD.i2c import CharLCD

class LCD:
    def __init__(self):
        self._lcd = CharLCD(
            i2c_expander='PCF8574',
            address=0x27,
            port=1,
            cols=16,
            rows=2,
            dotsize=8,
            charmap='A02',
            auto_linebreaks=True,
            backlight_enabled=True,
        )

    def write(self, msg:list) -> None:
        try:
            self._lcd.cursor_pos = (0,0)
            for string in msg:
                self._lcd.write_string(string)
                self._lcd.cursor_pos = (1,0)
            self._lcd.cursor_pos = (0,0)

        except:
            if len(msg) > 2:
                print("LCD:MSG OVERFLOW")

    def line_write(self, msg:str, row:int, col:int) -> None:
        try:
            self._lcd.cursor_pos = (row, col)
            if msg is not None and len(msg) > 0:
                self._lcd.write_string(msg)

        except:
            if row > 0 or col > 0:
                print("LCD:POSITION IS NOT DEFINED")
            elif msg is not None and len(msg) == 0:
                print("LCD:MSG LENGTH IS NOT BE 0")
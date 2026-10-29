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
        
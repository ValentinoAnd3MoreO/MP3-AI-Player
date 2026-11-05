import time

from machine import Pin, SPI


class VS1053:
    SCI_MODE = 0x00
    SCI_CLOCKF = 0x03
    SCI_VOL = 0x0B
    SM_RESET = 0x0004
    SM_SDINEW = 0x0800

    def __init__(
        self,
        spi_id,
        sck,
        mosi,
        miso,
        xcs,
        xdcs,
        dreq,
        reset,
    ):
        self.spi = SPI(
            spi_id,
            baudrate=1_000_000,
            polarity=0,
            phase=0,
            sck=Pin(sck),
            mosi=Pin(mosi),
            miso=Pin(miso),
        )
        self.xcs = Pin(xcs, Pin.OUT, value=1)
        self.xdcs = Pin(xdcs, Pin.OUT, value=1)
        self.dreq = Pin(dreq, Pin.IN)
        self.reset = Pin(reset, Pin.OUT, value=1)
        self.buffer = bytearray(32)

        self.reset.value(0)
        time.sleep_ms(20)
        self.reset.value(1)
        self.wait_ready()
        self.write_register(self.SCI_MODE, self.SM_SDINEW | self.SM_RESET)
        time.sleep_ms(2)
        self.wait_ready()
        self.write_register(self.SCI_CLOCKF, 0x6000)
        self.write_register(self.SCI_VOL, 0x2020)
        self.wait_ready()

    def wait_ready(self, timeout_ms=3000):
        started = time.ticks_ms()
        while not self.dreq.value():
            if time.ticks_diff(time.ticks_ms(), started) > timeout_ms:
                raise OSError("Timeout esperando DREQ del VS1053")
            time.sleep_ms(1)

    def write_register(self, address, value):
        self.wait_ready()
        self.xcs.value(0)
        self.spi.write(
            bytes(
                (
                    0x02,
                    address,
                    (value >> 8) & 0xFF,
                    value & 0xFF,
                )
            )
        )
        self.xcs.value(1)

    def send_audio(self, data):
        offset = 0
        while offset < len(data):
            self.wait_ready()
            size = min(32, len(data) - offset)
            self.xdcs.value(0)
            self.spi.write(data[offset : offset + size])
            self.xdcs.value(1)
            offset += size

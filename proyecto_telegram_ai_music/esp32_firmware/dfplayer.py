import time

from machine import Pin, UART

from protocol import build_frame


class DFPlayer:
    def __init__(self, uart_id, tx_pin, rx_pin, baudrate=9600, volume=22):
        self.uart = UART(
            uart_id,
            baudrate=baudrate,
            tx=Pin(tx_pin),
            rx=Pin(rx_pin),
            timeout=1000,
        )
        time.sleep_ms(1200)
        self._send(0x0C, 0)
        time.sleep_ms(1200)
        self._send(0x06, max(0, min(volume, 30)))

    def _send(self, command, parameter=0):
        self.uart.write(build_frame(command, parameter))
        time.sleep_ms(100)

    def play_folder_track(self, folder, track):
        if not 1 <= folder <= 99 or not 1 <= track <= 255:
            raise ValueError("Carpeta o número de canción fuera de rango.")
        self._send(0x0F, (folder << 8) | track)

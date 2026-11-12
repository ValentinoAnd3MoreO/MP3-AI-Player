import time

from machine import Pin, UART

from protocol import build_frame, extract_frames


class DFPlayer:
    def __init__(
        self, uart_id, tx_pin, rx_pin, baudrate=9600, volume=22, equalizer=5
    ):
        self.uart = UART(
            uart_id,
            baudrate=baudrate,
            tx=Pin(tx_pin),
            rx=Pin(rx_pin),
            timeout=1000,
        )
        self._rx_buffer = bytearray()
        self._finished_events = 0
        self._responses = []
        self.current_path = None
        self.paused = False
        time.sleep_ms(1200)
        self._send(0x0C, 0)
        time.sleep_ms(1200)
        self.volume = max(0, min(volume, 30))
        self._send(0x06, self.volume)
        self._send(0x07, max(0, min(equalizer, 5)))

    def _send(self, command, parameter=0, feedback=0):
        self.uart.write(build_frame(command, parameter, feedback))
        time.sleep_ms(100)

    def play_folder_track(self, folder, track):
        if not 1 <= folder <= 99 or not 1 <= track <= 255:
            raise ValueError("Carpeta o número de canción fuera de rango.")
        self.poll()
        self._finished_events = 0
        self._send(0x0F, (folder << 8) | track, feedback=1)
        time.sleep_ms(300)
        self.poll()
        self.current_path = "/{:02d}/{:03d}.mp3".format(folder, track)
        self.paused = False

    def poll(self):
        if self.uart.any():
            data = self.uart.read()
            if data:
                self._rx_buffer.extend(data)
                for frame in extract_frames(self._rx_buffer):
                    if frame[3] == 0x3D:
                        self._finished_events += 1
                        self.current_path = None
                        self.paused = False
                    else:
                        self._responses.append(frame)
                if len(self._responses) > 10:
                    self._responses = self._responses[-10:]

    def finished_event_count(self):
        self.poll()
        return self._finished_events

    def pause(self):
        self.poll()
        self._responses = []
        self._send(0x0E, feedback=1)
        status = self._query_status()
        if status != 2:
            raise RuntimeError(
                "El DFPlayer no confirmó la pausa (estado {}).".format(status)
            )
        self.paused = True

    def resume(self):
        self.poll()
        self._responses = []
        self._send(0x0D, feedback=1)
        status = self._query_status()
        if status != 1:
            raise RuntimeError(
                "El DFPlayer no confirmó la reanudación (estado {}).".format(status)
            )
        self.paused = False

    def stop(self):
        self.poll()
        self._responses = []
        self._send(0x16, feedback=1)
        status = self._query_status()
        if status != 0:
            raise RuntimeError(
                "El DFPlayer no confirmó la detención (estado {}).".format(status)
            )
        self.current_path = None
        self.paused = False

    def _query_status(self):
        self._send(0x42, feedback=1)
        deadline = time.ticks_add(time.ticks_ms(), 1200)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            self.poll()
            for index, frame in enumerate(self._responses):
                if frame[3] == 0x40:
                    del self._responses[: index + 1]
                    raise RuntimeError("El DFPlayer informó un error de comando.")
                if frame[3] == 0x42:
                    del self._responses[: index + 1]
                    return (frame[5] << 8) | frame[6]
            time.sleep_ms(20)
        raise RuntimeError("El DFPlayer no respondió a la consulta de estado.")

    def set_volume(self, volume):
        if not isinstance(volume, int) or isinstance(volume, bool):
            raise ValueError("El volumen debe ser un entero entre 0 y 30.")
        if not 0 <= volume <= 30:
            raise ValueError("El volumen debe estar entre 0 y 30.")
        self._send(0x06, volume, feedback=1)
        self.volume = volume
        print("Volumen DFPlayer actualizado:", self.volume)

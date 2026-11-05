import json
import socket
import time

import network
from machine import Pin

import config
from vs1053 import VS1053


def _send_all(sock, data):
    offset = 0
    while offset < len(data):
        sent = sock.send(data[offset:])
        if not sent:
            raise OSError("La conexión HTTP se cerró al enviar")
        offset += sent


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
        deadline = time.ticks_add(time.ticks_ms(), 20000)
        while not wlan.isconnected():
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                raise OSError("No se pudo conectar al Wi-Fi")
            time.sleep_ms(250)
    print("Wi-Fi conectado:", wlan.ifconfig()[0])


def request_small(method, path, body=None):
    sock = socket.socket()
    sock.settimeout(8)
    try:
        sock.connect((config.SERVER_HOST, config.SERVER_PORT))
        payload = json.dumps(body).encode() if body is not None else b""
        request = (
            "{} {} HTTP/1.1\r\n"
            "Host: {}:{}\r\n"
            "Authorization: Bearer {}\r\n"
            "Connection: close\r\n"
            "Content-Length: {}\r\n"
            "Content-Type: application/json\r\n\r\n"
        ).format(
            method,
            path,
            config.SERVER_HOST,
            config.SERVER_PORT,
            config.DEVICE_API_TOKEN,
            len(payload),
        ).encode() + payload
        _send_all(sock, request)
        response = b""
        while True:
            chunk = sock.recv(512)
            if not chunk:
                break
            response += chunk
            if len(response) > 8192:
                raise ValueError("Respuesta HTTP demasiado grande")
        header, body_bytes = response.split(b"\r\n\r\n", 1)
        status = int(header.split(b" ", 2)[1])
        if status < 200 or status >= 300:
            raise OSError("Servidor HTTP respondió {}".format(status))
        return json.loads(body_bytes.decode())
    finally:
        sock.close()


def _read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            break
        data.extend(part)
    return bytes(data)


def stream_mp3(url, player):
    prefix = "http://"
    if not url.startswith(prefix):
        raise ValueError("El stream debe usar HTTP dentro de la red local")
    address, path = url[len(prefix) :].split("/", 1)
    host, port_text = address.rsplit(":", 1)
    port = int(port_text)
    sock = socket.socket()
    sock.settimeout(15)
    try:
        sock.connect((host, port))
        request = (
            "GET /{} HTTP/1.1\r\n"
            "Host: {}:{}\r\n"
            "Authorization: Bearer {}\r\n"
            "Connection: close\r\n\r\n"
        ).format(
            path,
            host,
            port,
            config.DEVICE_API_TOKEN,
        )
        _send_all(sock, request.encode())

        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            part = sock.recv(1)
            if not part:
                raise OSError("Respuesta HTTP incompleta")
            header.extend(part)
            if len(header) > 8192:
                raise ValueError("Encabezado HTTP demasiado largo")

        status_line = bytes(header).split(b"\r\n", 1)[0]
        status = int(status_line.split(b" ", 2)[1])
        if status != 200:
            raise OSError("El servidor de audio respondió {}".format(status))

        id3_header = _read_exact(sock, 10)
        if len(id3_header) == 10 and id3_header[:3] == b"ID3":
            tag_size = (
                (id3_header[6] << 21)
                | (id3_header[7] << 14)
                | (id3_header[8] << 7)
                | id3_header[9]
            )
            if id3_header[5] & 0x10:
                tag_size += 10
            while tag_size > 0:
                part = sock.recv(min(1024, tag_size))
                if not part:
                    raise OSError("Etiqueta ID3 incompleta")
                tag_size -= len(part)
        else:
            player.send_audio(id3_header)

        while True:
            data = sock.recv(1024)
            if not data:
                break
            player.send_audio(data)
    finally:
        sock.close()


def main():
    connect_wifi()
    player = VS1053(
        config.SPI_ID,
        config.PIN_SCK,
        config.PIN_MOSI,
        config.PIN_MISO,
        config.PIN_XCS,
        config.PIN_XDCS,
        config.PIN_DREQ,
        config.PIN_RESET,
    )
    print("Reproductor VS1053 listo; esperando órdenes de Telegram.")
    while True:
        try:
            result = request_small("GET", "/api/device/command")
            command = result.get("command")
            if command:
                print("Reproduciendo:", command.get("title"), "-", command.get("artist"))
                stream_mp3(command["stream_url"], player)
                request_small(
                    "POST",
                    "/api/device/ack/{}".format(command["command_id"]),
                    {},
                )
        except Exception as exc:
            print("Error de reproducción/comunicación:", exc)
        time.sleep(config.POLL_SECONDS)


main()

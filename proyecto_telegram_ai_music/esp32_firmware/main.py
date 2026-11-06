import json
import socket
import time

import network

import config
from dfplayer import DFPlayer


def _constant_time_equal(left, right):
    if len(left) != len(right):
        return False
    difference = 0
    for index in range(len(left)):
        difference |= ord(left[index]) ^ ord(right[index])
    return difference == 0


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
        deadline = time.ticks_add(time.ticks_ms(), 20000)
        while not wlan.isconnected():
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                raise OSError("No se pudo conectar al Wi-Fi.")
            time.sleep_ms(250)
    print("ESP32 conectado:", wlan.ifconfig()[0])


def _read_request(connection):
    data = bytearray()
    header_end = -1
    content_length = 0
    while header_end < 0:
        part = connection.recv(512)
        if not part:
            raise ValueError("Solicitud HTTP incompleta.")
        data.extend(part)
        if len(data) > 4096:
            raise ValueError("Encabezado HTTP demasiado grande.")
        header_end = data.find(b"\r\n\r\n")

    header_bytes = bytes(data[:header_end])
    body = bytearray(data[header_end + 4 :])
    lines = header_bytes.decode().split("\r\n")
    request_parts = lines[0].split(" ")
    if len(request_parts) != 3:
        raise ValueError("Línea HTTP inválida.")

    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    content_length = int(headers.get("content-length", "0"))
    if content_length < 0 or content_length > 2048:
        raise ValueError("Cuerpo HTTP demasiado grande.")
    while len(body) < content_length:
        part = connection.recv(min(512, content_length - len(body)))
        if not part:
            raise ValueError("Cuerpo HTTP incompleto.")
        body.extend(part)

    return request_parts[0], request_parts[1], headers, bytes(body[:content_length])


def _respond(connection, status, payload):
    body = json.dumps(payload).encode()
    reason = "OK" if status == 200 else "Bad Request" if status == 400 else "Unauthorized"
    response = (
        "HTTP/1.1 {} {}\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: {}\r\n"
        "Connection: close\r\n\r\n"
    ).format(status, reason, len(body)).encode() + body
    remaining = memoryview(response)
    while remaining:
        sent = connection.send(remaining)
        if not sent:
            raise OSError("La conexión HTTP se cerró antes de responder.")
        remaining = remaining[sent:]


def _handle(connection, player):
    try:
        method, path, headers, body = _read_request(connection)
        supplied = headers.get("authorization", "")
        expected = "Bearer " + config.API_TOKEN
        if not _constant_time_equal(supplied, expected):
            _respond(connection, 401, {"accepted": False, "error": "unauthorized"})
            return
        if method != "POST":
            _respond(connection, 400, {"accepted": False, "error": "invalid_route"})
            return

        if path == "/api/pause":
            player.pause()
            _respond(connection, 200, {"accepted": True, "action": "pause"})
            return
        if path == "/api/resume":
            player.resume()
            _respond(connection, 200, {"accepted": True, "action": "resume"})
            return
        if path != "/api/play":
            _respond(connection, 400, {"accepted": False, "error": "invalid_route"})
            return

        payload = json.loads(body.decode())
        if not isinstance(payload, dict):
            _respond(connection, 400, {"accepted": False, "error": "invalid_payload"})
            return
        sd_path = payload.get("sd_path")
        if not isinstance(sd_path, str):
            _respond(connection, 400, {"accepted": False, "error": "invalid_sd_path"})
            return
        parts = sd_path.split("/")
        if (
            len(parts) != 3
            or not parts[1].isdigit()
            or len(parts[1]) != 2
            or not parts[2].lower().endswith(".mp3")
            or not parts[2][:-4].isdigit()
            or len(parts[2][:-4]) != 3
        ):
            _respond(connection, 400, {"accepted": False, "error": "invalid_sd_path"})
            return
        folder = int(parts[1])
        track = int(parts[2][:-4])
        if not 1 <= folder <= 99 or not 1 <= track <= 255:
            _respond(connection, 400, {"accepted": False, "error": "invalid_sd_path"})
            return

        player.play_folder_track(folder, track)
        print("Reproduciendo ruta SD:", sd_path)
        _respond(connection, 200, {"accepted": True, "sd_path": sd_path})
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print("Error de solicitud:", exc)
        try:
            _respond(connection, 400, {"accepted": False, "error": "bad_request"})
        except OSError as response_error:
            print("No se pudo responder al cliente:", response_error)


def main():
    if not config.API_TOKEN or len(config.API_TOKEN) < 24:
        raise ValueError("Configura un API_TOKEN aleatorio de al menos 24 caracteres.")
    connect_wifi()
    player = DFPlayer(
        config.UART_ID,
        config.UART_TX_PIN,
        config.UART_RX_PIN,
        config.UART_BAUDRATE,
        config.PLAYER_VOLUME,
    )

    address = socket.getaddrinfo("0.0.0.0", config.HTTP_PORT)[0][-1]
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(address)
    server.listen(2)
    print("Servidor del DFPlayer escuchando en el puerto", config.HTTP_PORT)

    while True:
        connection, remote = server.accept()
        try:
            connection.settimeout(4)
            _handle(connection, player)
        finally:
            connection.close()


main()



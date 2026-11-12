def build_frame(command, parameter=0, feedback=0):
    parameter_high = (parameter >> 8) & 0xFF
    parameter_low = parameter & 0xFF
    checksum = (
        -(0xFF + 0x06 + command + feedback + parameter_high + parameter_low)
    ) & 0xFFFF
    return bytes(
        (
            0x7E,
            0xFF,
            0x06,
            command,
            feedback,
            parameter_high,
            parameter_low,
            (checksum >> 8) & 0xFF,
            checksum & 0xFF,
            0xEF,
        )
    )


def extract_frames(buffer):
    frames = []
    while True:
        start = buffer.find(b"\x7e")
        if start < 0:
            buffer.clear()
            return frames
        if start:
            del buffer[:start]
        if len(buffer) < 10:
            return frames
        if buffer[9] != 0xEF:
            del buffer[0]
            continue
        if buffer[1] != 0xFF or buffer[2] != 0x06:
            del buffer[0]
            continue
        frames.append(bytes(buffer[:10]))
        del buffer[:10]


def extract_finished_events(buffer):
    return sum(frame[3] == 0x3D for frame in extract_frames(buffer))

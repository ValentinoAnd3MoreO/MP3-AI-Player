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

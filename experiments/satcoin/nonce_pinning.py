"""Single source of truth for the nonce <-> CNF-variable mapping.

This module exists because there's exactly one easy mistake worth coding
around: treating the nonce as an integer and serializing it big-endian
("but it IS the integer 0x08ffbdee — let me write that as 08 ff bd ee").

cgen's M template puts header bytes 76..79 at CNF variables 1..32,
MSB-first within each byte, in NETWORK ORDER. The bitcoin nonce field
is encoded little-endian in the header — so bytes 76..79 on the wire
are the LE serialization of the displayed nonce. Use the raw header
hex string, not the displayed integer.

Use these helpers everywhere; do not re-derive inline.
"""

from __future__ import annotations


def nonce_bytes_from_header(raw_header_hex: str) -> bytes:
    """Extract bytes 76..79 of the header (the nonce field) in network order.

    This is the only correct source for the bits that go into CNF variables
    1..32. NEVER use `int.to_bytes(4, 'big')` of the displayed nonce value —
    that gives the byte-reversed sequence and will cause UNSAT.
    """
    if len(raw_header_hex) != 160:
        raise ValueError(f"raw_header_hex must be 160 chars, got {len(raw_header_hex)}")
    return bytes.fromhex(raw_header_hex[152:160])


def unit_clauses_for_nonce(nonce_bytes_network_order: bytes) -> list[int]:
    """Convert 4 nonce bytes (bytes 76..79 in network order) into 32 CNF unit
    clauses pinning variables 1..32 to the corresponding bits.

    Layout: var i for i in [1..32] is the (MSB-first) bit at position
    (i-1) % 8 of byte 76 + (i-1)//8.

    Returns: list of 32 signed ints, ready to be appended to a DIMACS file
    (each followed by ' 0\\n').
    """
    if len(nonce_bytes_network_order) != 4:
        raise ValueError(f"nonce must be 4 bytes, got {len(nonce_bytes_network_order)}")
    units: list[int] = []
    for byte_i, b in enumerate(nonce_bytes_network_order):
        for bit_in_byte in range(7, -1, -1):  # MSB-first
            var = byte_i * 8 + (7 - bit_in_byte) + 1
            sign = 1 if (b >> bit_in_byte) & 1 else -1
            units.append(sign * var)
    return units


def pin_nonce_in_cnf(cnf_in_path: str, cnf_out_path: str, raw_header_hex: str) -> int:
    """Read cnf_in_path, append 32 unit clauses pinning the nonce extracted
    from raw_header_hex, write to cnf_out_path. Updates the 'p cnf' header
    line's clause count. Returns the number of unit clauses appended (always 32).
    """
    units = unit_clauses_for_nonce(nonce_bytes_from_header(raw_header_hex))
    with open(cnf_in_path, "r") as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        if line.startswith("p cnf"):
            parts = line.split()
            lines[i] = f"p cnf {parts[2]} {int(parts[3]) + len(units)}\n"
            break
    lines.append("c PINNED: 32 unit clauses for nonce (network-order bytes 76..79)\n")
    for u in units:
        lines.append(f"{u} 0\n")
    with open(cnf_out_path, "w") as f:
        f.writelines(lines)
    return len(units)


def nonce_int_from_assignment(assignment: dict[int, bool]) -> int:
    """Inverse: given a CNF variable assignment dict (var -> bool), return
    the nonce as the displayed little-endian integer (matches block explorer)."""
    nonce_bytes = bytearray(4)
    for byte_i in range(4):
        b = 0
        for bit_in_byte in range(7, -1, -1):
            var = byte_i * 8 + (7 - bit_in_byte) + 1
            if assignment.get(var):
                b |= (1 << bit_in_byte)
        nonce_bytes[byte_i] = b
    return int.from_bytes(bytes(nonce_bytes), "little")

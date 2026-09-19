"""Capture a home key by impersonating a shade that has never been adopted.

A shade leaves the factory with a zeroed home key, so the write that
installs one arrives in the clear. Presenting an unadopted shade to the
PowerView app therefore yields the key for the whole home, which is what
the ESP32 and Linux emulators under `emu/` do out-of-process.

This module carries the half of that which needs no Bluetooth: framing a
reply and recognising the key write. The BlueZ transport that puts it on
the air lives separately, so the part that cannot be exercised without a
Linux host and a local adapter stays as small as possible.

Framing mirrors `PowerViewBLE` in api.py rather than either emulator --
those are GPLv2 and this ships under the repository's Apache licence.
"""

from __future__ import annotations

from dataclasses import dataclass
import sys
from typing import Final

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from habluetooth import HaScanner

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback

from .const import LOGGER

# Request layout is serviceID, cmdID, sequence, length, body -- the frame
# _transact() builds. A reply echoes the first three with 0x10 cleared from
# the serviceID, which is the same mask api.py applies as `cmd & 0xFFEF`.
HEADER_LEN: Final[int] = 4
RESPONSE_MASK: Final[int] = 0xEF
KEY_LEN: Final[int] = 16

# serviceID 0xFB, cmdID 0x02: the write that installs a home key.
SET_KEY_CMD: Final[tuple[int, int]] = (0xFB, 0x02)

# Status byte of a bare acknowledgement. 0x00 is success; shades answer
# 0x04 for a bad length and 0x80 for a bad field (PV_ERROR_CODES in
# scripts/shade_report.py).
ACK_OK: Final[bytes] = b"\x00"


@dataclass(frozen=True)
class CaptureSupport:
    """Whether this installation can advertise a shade of its own."""

    supported: bool
    reason: str | None = None
    adapter: str | None = None


@callback
def async_capture_support(hass: HomeAssistant) -> CaptureSupport:
    """Report whether a home key can be captured on this installation.

    Checks only what is cheap and decisive, so the offer can be hidden
    rather than failing later: BlueZ is Linux-only, and a Bluetooth proxy
    can scan but never advertise, so an install with no local adapter
    cannot do this however modern its proxies are.

    Whether the adapter supports extended advertising is deliberately not
    checked here -- it needs the adapter's own details, and the payload is
    over the 31-byte legacy limit, so registration is the honest test.
    """
    if not sys.platform.startswith("linux"):
        return CaptureSupport(False, "capture_not_linux")

    local = [
        scanner
        for scanner in bluetooth.async_current_scanners(hass)
        if isinstance(scanner, HaScanner)
    ]
    if not local:
        return CaptureSupport(False, "capture_no_local_adapter")

    return CaptureSupport(True, adapter=local[0].adapter)


class ShadeResponder:
    """Answer writes as an unadopted shade would, keeping any key offered.

    Starts with no key, so traffic is plaintext in both directions. The
    key write is acknowledged *before* the key takes effect, because the
    other side does not know the shade has it yet and would be unable to
    read an encrypted reply.
    """

    def __init__(self) -> None:
        """Start unadopted, with no key and no encryption."""
        self._key: bytes = b""

    @property
    def home_key(self) -> bytes:
        """Return the captured key, or empty until one has been installed."""
        return self._key

    def _crypt(self, data: bytes) -> bytes:
        # AES-128-CTR with a zero nonce, a fresh context per message so the
        # counter restarts each time -- the construction api.py connects with.
        ctx = Cipher(algorithms.AES(self._key), modes.CTR(bytes(16))).encryptor()
        return ctx.update(data) + ctx.finalize()

    def handle(self, data: bytes) -> bytes | None:
        """Return the reply to one written frame, or None if unreadable.

        Captures the home key as a side effect when the frame is the write
        that installs one.
        """
        if len(data) < HEADER_LEN:
            LOGGER.debug("keycapture: runt frame (%d bytes)", len(data))
            return None

        plain = self._crypt(data) if self._key else data
        service_id, cmd_id, sequence, data_len = plain[:HEADER_LEN]
        body = plain[HEADER_LEN : HEADER_LEN + data_len]
        LOGGER.debug(
            "keycapture: srv %02x cmd %02x seq %d len %d",
            service_id,
            cmd_id,
            sequence,
            data_len,
        )

        reply = bytes(
            [service_id & RESPONSE_MASK, cmd_id, sequence, len(ACK_OK)]
        ) + ACK_OK
        # Encrypt against the key in force as the frame arrived, so the
        # acknowledgement below still goes out in the clear.
        if self._key:
            reply = self._crypt(reply)

        if (service_id, cmd_id) == SET_KEY_CMD:
            self._install_key(body, data_len)

        return reply

    def _install_key(self, body: bytes, data_len: int) -> None:
        if data_len != KEY_LEN or len(body) != KEY_LEN:
            LOGGER.warning(
                "keycapture: ignoring %d-byte home key, expected %d",
                len(body),
                KEY_LEN,
            )
            return
        self._key = bytes(body)
        LOGGER.info("keycapture: home key captured")

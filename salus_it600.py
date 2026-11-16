"""ZHA custom quirk for Salus iT600 "Quantum" thermostats and the KL08RF wiring centre.

Copyright (c) 2026 Maciej Kornacki
Copyright (c) 2026 Mikkel Eskildsen
SPDX-License-Identifier: MIT (see LICENSE)

Ported from the zigbee2mqtt converter and protocol notes by Mikkel Eskildsen at
https://github.com/mikkelke/salus-zigbee (MIT). The protocol knowledge, byte layouts and
command sequences used here come from that work. Section numbers ("PROTOCOL 5.2") refer
to PROTOCOL.md in that repository.

Not affiliated with or endorsed by Salus Controls or Computime. Provided "as is",
without warranty of any kind; use it at your own risk. The authors are not liable for
any damage or loss from its use. See the Disclaimer in README.md and the LICENSE.

The devices (Computime, manufacturer code 0x1078) only work properly when the
coordinator behaves like a Salus gateway:

* They greet the coordinator with FC00 command 0x10 and wait for a 0x81 sync response
  that gives them a device number and the local time (PROTOCOL 4).
* The thermostat pushes its state as FC00 0x31 records: 0x71 live status and
  0x72 settings (PROTOCOL 5).
* Writes are FC00 0x32 "B-commands", echoed back as 0x33 (PROTOCOL 6).

This quirk answers the greeting and maps the records onto standard ZCL clusters held
locally (Thermostat, RelativeHumidity, PowerConfiguration), so ZHA builds a normal
climate entity, a humidity sensor and, for the battery model, a battery sensor.
Settings get their own entities. The mains-powered SQ610RF and the KL08RF are Zigbee
routers, so once they have joined ZHA they extend the mesh.

Install: put this file in the ZHA custom quirks folder, for example
/config/custom_zha_quirks/, with this in configuration.yaml:

    zha:
      custom_quirks_path: /config/custom_zha_quirks/

then restart Home Assistant and pair the devices again (README.md has the details).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import importlib.metadata
import importlib.util
import logging
import time

from zigpy import types as t
from zigpy.profiles import zha as zha_profile
from zigpy.zcl import ClusterType, foundation
from zigpy.zcl.clusters.general import Basic, PowerConfiguration
from zigpy.zcl.clusters.hvac import (
    ControlSequenceOfOperation,
    RunningState,
    SystemMode,
    Thermostat,
)
from zigpy.zcl.clusters.measurement import RelativeHumidity
from zigpy.zcl.foundation import (
    BaseAttributeDefs,
    BaseCommandDefs,
    ZCLAttributeDef,
    ZCLCommandDef,
)

from zhaquirks import LocalDataCluster

_LOGGER = logging.getLogger(__name__)


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


_VERSIONS = ", ".join(f"{p} {_version(p)}" for p in ("zigpy", "zha-quirks", "zha"))

# The quirks v2 API moved from zigpy.quirks.v2 to zhaquirks.builder in zha-quirks 2.0.
# Look for it first instead of catching an ImportError, so the log of an older release
# does not show a misleading "No module named 'zhaquirks.builder'".
if importlib.util.find_spec("zhaquirks.builder") is not None:  # zha-quirks >= 2.0
    from zhaquirks.builder import EntityType, QuirkBuilder, SensorDeviceClass
    from zhaquirks.builder import UnitOfTemperature
elif importlib.util.find_spec("zigpy.quirks.v2.homeassistant") is not None:  # older
    from zigpy.quirks.v2 import EntityType, QuirkBuilder  # type: ignore[no-redef]
    from zigpy.quirks.v2.homeassistant import UnitOfTemperature  # type: ignore[no-redef]
    from zigpy.quirks.v2.homeassistant.sensor import (  # type: ignore[no-redef]
        SensorDeviceClass,
    )
else:
    raise ImportError(
        "salus_it600: no quirks v2 API found (neither zhaquirks.builder nor "
        f"zigpy.quirks.v2). Installed: {_VERSIONS}. These versions do not match each "
        "other; reinstall the versions your Home Assistant release pins."
    )


COMPUTIME = 0x1078
SALUS_CLUSTER_ID = 0xFC00
COORDINATOR_ENDPOINT = 1
THERMOSTAT_ENDPOINT = 9
WIRING_CENTRE_ENDPOINT = 8

# Device number in byte 0 of the sync response (PROTOCOL 4.3). The wiring centre keeps
# the first number it gets and refuses zone pairing unless it is a wiring centre's (1-9).
THERMOSTAT_NUMBER = 0x20
WIRING_CENTRE_NUMBER = 1

THERMOSTAT_MODELS = ("SQ610RF", "SQ610RFNH", "SQ610", "SQ610NH")
# Basic manufacturerName values Salus devices are known or likely to report.
MANUFACTURER_NAMES = ("Computime", "Salus", "SalusControls", "Salus Controls")
BATTERY_MODELS = ("SQ610RFNH",)

# FC00 command ids (PROTOCOL 2).
CMD_SYNC_REQUEST = 0x10
CMD_ZONE_REPORT = 0x12
CMD_WC_REQUEST = 0x16
CMD_REPLACED_THERMOSTAT = 0x1A
CMD_RECORD = 0x31
CMD_WRITE = 0x32
CMD_ECHO = 0x33
CMD_SYNC_ACK = 0x37
CMD_SYNC_RESPONSE = 0x81
CMD_WC_RESPONSE = 0x86

RECORD_STATUS = 0x71
RECORD_SETTINGS = 0x72
STATUS_MIN_LENGTH = 50
SETTINGS_LENGTH = 48
PLACEHOLDER_TEMPERATURE = 0x1234

ECHO_TIMEOUT = 30
SETTINGS_TIMEOUT = 30
ZONE_PAIRING_TIMEOUT_PER_ZONE = 20
SYNC_INTERVAL = 24 * 3600
# A device seen this recently when the quirk is applied has just joined: greet it.
JOIN_WINDOW = 120
JOIN_GREETING_DELAY = 3

# Time zone table of the Salus gateway, standard UTC offset in seconds (PROTOCOL 4.2).
TIME_ZONE_OFFSETS = [
    -46800, -43200, -39600, -36000, -32400, -28800, -25200, -21600, -18000, -14400,
    -10800, -7200, -3600, 0, 3600, 7200, 10800, 14400, 18000, 21600, 25200, 28800,
    32400, 36000, 39600, 43200, 46800, 50400, -34200, -16200, -12600, -11220, 12600,
    16200, 19800, 20700, 23400, 31500, 34200, 37800, 41400, 45900,
]  # fmt: skip
SECONDS_1970_TO_2000 = 946684800

# Offsets in a 0x71 status record (PROTOCOL 5.2).
ST_HOLD_TYPE = 3
ST_TEMPERATURE = 4
ST_SETPOINT = 9
ST_SCHEDULE_SETPOINT = 11
ST_SYSTEM_MODE = 15
ST_RUNNING_STATE = 17
ST_KEY_LOCK = 23
ST_HUMIDITY = 24
ST_BATTERY = 49

# Offsets in a 0x72 settings record (PROTOCOL 5.3).
SE_COUNTER = 1
SE_TEMPERATURE_OFFSET = 3
SE_FROST_SETPOINT = 6
SE_DISPLAY_RESOLUTION = 11
SE_HEAT_CONTROL = 12
SE_MAX_HEAT_SETPOINT = 22
SE_MIN_HEAT_SETPOINT = 24
SE_MAX_COOL_SETPOINT = 26
SE_MIN_COOL_SETPOINT = 28
SE_PIN = 40
B11_USER = 0x72  # 'r': user settings only
B11_ADMIN = 0x73  # 's': user and admin settings
NO_PIN = 0xFFFF
NO_PIN_BYTES = (0x01, 0x01, 0xFF)  # counter, PIN high, PIN low: decodes to no PIN

SYSTEM_MODE_COOL = 3
SYSTEM_MODE_HEAT = 4
BATTERY_PERCENT = (0, 10, 25, 50, 75, 100)

# The wiring centre's zone table (PROTOCOL 8) and zone pairing (PROTOCOL 7.1).
ZONES = 8
ZONE_TABLE_FIRST_SLOT = 10
ZONE_TABLE_SLOT_LENGTH = 3
ZONE_TABLE_LENGTH = ZONE_TABLE_FIRST_SLOT + ZONE_TABLE_SLOT_LENGTH * ZONES
ZONE_TABLE_REQUEST = b"B70" + bytes([0x7E, 0xAA, 0xAA, 0xA8, 0x80, 0x00, 0x00, 0x00, 0x78])
FREE_ZONE = 0xFFFF
B47_LENGTH = 33


class HoldType(t.enum8):
    """Hold type of the thermostat (0x71 byte 3), set with B20."""

    schedule = 0x00
    temporary_hold = 0x01
    permanent_hold = 0x02
    away = 0x06
    standby = 0x07
    eco = 0x0A


SETTABLE_HOLD_TYPES = {HoldType.schedule, HoldType.permanent_hold, HoldType.away}


class HeatControl(t.enum8):
    """Heat control algorithm (0x72 byte 12)."""

    itlc_underfloor = 0x00
    itlc_radiator = 0x01
    itlc_electric = 0x02
    span_0_25 = 0x03
    span_0_5 = 0x04
    thb_actuator = 0x05


class DisplayResolution(t.enum8):
    """Temperature step on the display (0x72 byte 11)."""

    half_degree = 0x00
    tenth_degree = 0x01


class SalusError(Exception):
    """A Salus command failed."""


def _bcd(byte: int) -> int:
    return (byte >> 4) * 10 + (byte & 0x0F)


def _to_bcd(value: int) -> int:
    return ((value // 10) << 4) | (value % 10)


def bcd_x100(record: bytes, offset: int) -> int:
    """Four BCD digits holding the value x100; negatives are 11000 - raw."""
    raw = _bcd(record[offset]) * 100 + _bcd(record[offset + 1])
    return 11000 - raw if raw >= 11000 else raw


def bcd_x100_bytes(hundredths: int) -> bytes:
    """Encode a non-negative value x100 as four BCD digits."""
    return bytes([_to_bcd(hundredths // 100), _to_bcd(hundredths % 100)])


def signed_bcd_x100(record: bytes, offset: int) -> int:
    """The 0x72 offset encoding: bit 7 of the first byte is the sign."""
    hundredths = _bcd(record[offset] & 0x7F) * 100 + _bcd(record[offset + 1])
    return -hundredths if record[offset] & 0x80 else hundredths


def signed_bcd_x100_bytes(hundredths: int) -> bytes:
    """Encode a value x100 with the sign in bit 7."""
    high, low = bcd_x100_bytes(abs(hundredths))
    return bytes([high | 0x80 if hundredths < 0 else high, low])


def setpoint_byte(hundredths: int) -> int:
    """B-command setpoint byte: 0.5 degree steps offset by 0x20 (PROTOCOL 6.2)."""
    return round(hundredths / 50) + 0x20


def pin_of(record: bytes) -> int:
    """Decode the PIN in 0x72 bytes 40-41, scrambled with the counter in byte 1."""
    counter = record[SE_COUNTER]
    high = (record[SE_PIN] - 2 * counter) & 0xFF
    low = (record[SE_PIN + 1] + (counter >> 2)) & 0xFF
    return (high << 8) | low


def _local_zone() -> dt.tzinfo | None:
    """Home Assistant's configured time zone, else the host's."""
    try:
        from homeassistant.util import dt as dt_util  # noqa: PLC0415
    except ImportError:
        return None
    return dt_util.get_default_time_zone()


def _offset(moment: dt.datetime) -> int:
    offset = moment.utcoffset()
    return int(offset.total_seconds()) if offset is not None else 0


def sync_response_payload(number: int, now: dt.datetime | None = None) -> bytes:
    """Build the 15-byte 0x81 sync response (PROTOCOL 4.2)."""
    zone = _local_zone()
    if now is None:
        now = dt.datetime.now(dt.UTC)
    local = now.astimezone(zone) if zone else now.astimezone()
    offset = _offset(local)
    tz = local.tzinfo
    offsets = [
        _offset(dt.datetime(local.year, month, 1, 12, tzinfo=dt.UTC).astimezone(tz))
        for month in (1, 7)
    ]
    standard, summer = min(offsets), max(offsets)
    if standard in TIME_ZONE_OFFSETS:
        tz_index = TIME_ZONE_OFFSETS.index(standard)
    else:
        tz_index = min(
            range(len(TIME_ZONE_OFFSETS)),
            key=lambda i: abs(TIME_ZONE_OFFSETS[i] - standard),
        )

    payload = bytearray(15)
    payload[0] = number
    local_seconds = int(now.timestamp()) + offset - SECONDS_1970_TO_2000
    payload[2:6] = local_seconds.to_bytes(4, "little")
    payload[6] = 0x02 | (0x01 if offset > standard else 0x00)  # 24 h clock, DST
    payload[7] = tz_index
    payload[8] = round((summer - standard) / 3600)
    payload[9:13] = b"\xff\xff\xff\xff"  # no paired receivers
    payload[13] = 0x31  # '1': cloud online
    return bytes(payload)


async def send_salus_frame(
    endpoint, command_id: int, payload: bytes, *, tsn: int | None = None
) -> None:
    """Send an FC00 frame the way the Salus gateway does.

    Frame control 0x1D (cluster specific, manufacturer specific, server to client,
    no default response), manufacturer 0x1078, from coordinator endpoint 1.
    """
    device = endpoint.device
    if tsn is None:
        tsn = device.get_sequence()
    hdr = foundation.ZCLHeader.cluster(
        tsn=tsn,
        command_id=command_id,
        manufacturer=COMPUTIME,
        direction=foundation.Direction.Server_to_Client,
    )
    await device.request(
        profile=zha_profile.PROFILE_ID,
        cluster=SALUS_CLUSTER_ID,
        src_ep=COORDINATOR_ENDPOINT,
        dst_ep=endpoint.endpoint_id,
        sequence=tsn,
        data=hdr.serialize() + payload,
        expect_reply=False,
    )


def _is_wiring_centre(device) -> bool:
    endpoint = device.endpoints.get(WIRING_CENTRE_ENDPOINT)
    return (
        endpoint is not None
        and not isinstance(endpoint, int)
        and isinstance(
            endpoint.in_clusters.get(SALUS_CLUSTER_ID), SalusWiringCentreCluster
        )
    )


def find_wiring_centre(application):
    """The wiring centre on this network (one is supported, PROTOCOL 4.3)."""
    for device in application.devices.values():
        if _is_wiring_centre(device):
            return device
    return None


def _address_name(application, nwk: int | None) -> str:
    if nwk is None:
        return "free"
    for device in application.devices.values():
        if device.nwk == nwk:
            return str(device.ieee)
    return f"0x{nwk:04x}"


# Zone pairings waiting for the wiring centre's zone table: (thermostat nwk, zones, future).
_zone_pairings: set[tuple[int, tuple[int, ...], asyncio.Future]] = set()


class SalusBaseCluster(LocalDataCluster):
    """Common handling of the Salus FC00 cluster.

    Incoming FC00 frames are taken raw, whatever their direction bit, and handled in
    `handle_salus_frame`. The attributes of this cluster are virtual: they hold what
    the device reported and are written with B-commands.
    """

    cluster_id = SALUS_CLUSTER_ID
    name = "Salus iT600"
    ep_attribute = "salus_it600"
    device_number = THERMOSTAT_NUMBER

    def __init__(self, *args, **kwargs):
        """Init."""
        super().__init__(*args, **kwargs)
        self._handle = 0
        self._echoes: dict[int, tuple[str, asyncio.Future]] = {}
        self._lock = asyncio.Lock()
        self._last_sync: tuple[float, int] | None = None
        self._schedule_join_greeting()

    # --- incoming frames -------------------------------------------------------

    def deserialize(self, data: bytes):
        """Return cluster-specific frames as raw payload bytes."""
        hdr, rest = foundation.ZCLHeader.deserialize(data)
        if hdr.frame_control.frame_type == foundation.FrameType.CLUSTER_COMMAND:
            return hdr, bytes(rest)
        return super().deserialize(data)

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        """Dispatch a raw FC00 frame."""
        payload = bytes(args) if isinstance(args, bytes | bytearray) else b""
        self.debug("Salus frame 0x%02x: %s", hdr.command_id, payload.hex(" "))
        if hdr.command_id == CMD_SYNC_REQUEST:
            self.create_catching_task(self.send_sync())
        elif hdr.command_id == CMD_ECHO:
            self._resolve_echo(payload)
            self.handle_echo(payload)
        elif hdr.command_id == CMD_WC_REQUEST and payload:
            self.create_catching_task(self._answer_wiring_centre_request(payload[0]))
        else:
            self.handle_salus_frame(hdr.command_id, payload)

    def handle_salus_frame(self, command_id: int, payload: bytes) -> None:
        """Handle a device-specific frame."""

    def handle_echo(self, payload: bytes) -> None:
        """Handle a 0x33 echo or report."""

    # --- greeting --------------------------------------------------------------

    def _schedule_join_greeting(self) -> None:
        """Greet a device that joined just now.

        The device sends its 0x10 while ZHA is still interviewing it, before this quirk
        exists, so the greeting would be lost. When the quirk is applied to a device that
        was seen moments ago, the sync response is sent unprompted, as the gateway does.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        last_seen = getattr(self.endpoint.device, "last_seen", None)
        if last_seen is None:
            return
        if isinstance(last_seen, dt.datetime):
            last_seen = last_seen.timestamp()
        if time.time() - last_seen > JOIN_WINDOW:
            return

        async def greet() -> None:
            await asyncio.sleep(JOIN_GREETING_DELAY)
            with contextlib.suppress(Exception):
                await self.send_sync()

        self.create_catching_task(greet())

    async def send_sync(self) -> None:
        """Send the 0x81 sync response: device number, local time and time zone."""
        payload = sync_response_payload(self.device_number)
        self._last_sync = (time.monotonic(), _offset(dt.datetime.now().astimezone()))
        try:
            await send_salus_frame(self.endpoint, CMD_SYNC_RESPONSE, payload)
        except Exception as exc:  # noqa: BLE001
            self._last_sync = None
            self.warning("Sync response failed: %r", exc)
        else:
            self.debug("Sent sync response %s", payload.hex(" "))

    def resync_clock_if_due(self) -> None:
        """Resend the sync response daily and when summer time starts or ends."""
        now_offset = _offset(dt.datetime.now().astimezone())
        if (
            self._last_sync is None
            or time.monotonic() - self._last_sync[0] > SYNC_INTERVAL
            or self._last_sync[1] != now_offset
        ):
            self.create_catching_task(self.send_sync())

    async def _answer_wiring_centre_request(self, number: int) -> None:
        """Answer 0x16 with 0x86: 01 + the wiring centre's short address, or 00 ff ff."""
        payload = b"\x00\xff\xff"
        wiring_centre = find_wiring_centre(self.endpoint.device.application)
        if number == WIRING_CENTRE_NUMBER and wiring_centre is not None:
            payload = b"\x01" + int(wiring_centre.nwk).to_bytes(2, "little")
        await send_salus_frame(self.endpoint, CMD_WC_RESPONSE, payload)

    # --- B-commands ------------------------------------------------------------

    def _next_handle(self) -> int:
        """Handles 1-127; the device uses 0 and 0x80+ for its own reports."""
        for _ in range(0x7F):
            self._handle = self._handle % 0x7F + 1
            if self._handle not in self._echoes:
                return self._handle
        raise SalusError("All 127 B-command handles are waiting for an echo")

    def _resolve_echo(self, payload: bytes) -> None:
        if len(payload) < 4:
            return
        pending = self._echoes.get(payload[0])
        if pending and pending[0] == payload[1:4].decode("latin1") and not pending[1].done():
            pending[1].set_result(payload)

    async def b_command(self, code: str, args: bytes = b"", *, wait: bool = True) -> bytes:
        """Send '<handle> B.. <args>' as 0x32 and wait for the device's 0x33 echo."""
        handle = self._next_handle()
        future = asyncio.get_running_loop().create_future()
        self._echoes[handle] = (code, future)
        try:
            await send_salus_frame(
                self.endpoint, CMD_WRITE, bytes([handle]) + code.encode("latin1") + args
            )
            if not wait:
                return b""
            try:
                return await asyncio.wait_for(future, ECHO_TIMEOUT)
            except TimeoutError as exc:
                raise SalusError(f"No {code} echo within {ECHO_TIMEOUT} s") from exc
        finally:
            self._echoes.pop(handle, None)

    async def send_b_command(self, hex_payload: str) -> None:
        """Send a raw B-command given as hex without the handle, e.g. '42323101'."""
        try:
            data = bytes.fromhex(str(hex_payload).replace(" ", ""))
        except ValueError as exc:
            raise SalusError(f"Not hex in whole bytes: {hex_payload!r}") from exc
        if len(data) < 3 or data[0] != 0x42:
            raise SalusError("A B-command starts with 42 ('B') and two code bytes")
        echo = await self.b_command(data[:3].decode("latin1"), data[3:])
        self.info("B-command echo: %s", echo.hex(" "))


class SalusThermostatCluster(SalusBaseCluster):
    """FC00 cluster of an SQ610 thermostat."""

    class AttributeDefs(BaseAttributeDefs):
        """Virtual attributes holding what the thermostat reported."""

        hold_type = ZCLAttributeDef(id=0x0001, type=HoldType, access="rw")
        child_lock = ZCLAttributeDef(id=0x0002, type=t.Bool, access="rw")
        schedule_setpoint = ZCLAttributeDef(id=0x0003, type=t.int16s, access="r")
        frost_setpoint = ZCLAttributeDef(id=0x0004, type=t.int16s, access="rw")
        temperature_offset = ZCLAttributeDef(id=0x0005, type=t.int16s, access="rw")
        heat_control = ZCLAttributeDef(id=0x0006, type=HeatControl, access="rw")
        display_resolution = ZCLAttributeDef(id=0x0007, type=DisplayResolution, access="rw")
        system_mode_raw = ZCLAttributeDef(id=0x000A, type=t.uint8_t, access="r")

    _VALID_ATTRIBUTES = {attr.id for attr in AttributeDefs}

    class ServerCommandDefs(BaseCommandDefs):
        """Actions for Developer tools -> zha.issue_zigbee_cluster_command (not sent as is)."""

        pair_wiring_centre_zones = ZCLCommandDef(
            id=0xE0, schema={"zones": t.CharacterString, "replace": t.Bool}
        )
        send_b_command = ZCLCommandDef(id=0xE1, schema={"hex_payload": t.CharacterString})
        refresh_settings = ZCLCommandDef(id=0xE2, schema={})

    def __init__(self, *args, **kwargs):
        """Init."""
        super().__init__(*args, **kwargs)
        self._settings: bytes | None = None
        self._settings_waiter: asyncio.Future | None = None
        self._initial_settings_requested = False

    @property
    def _thermostat(self) -> SalusThermostat:
        return self.endpoint.in_clusters[Thermostat.cluster_id]

    # --- records ---------------------------------------------------------------

    def handle_salus_frame(self, command_id: int, payload: bytes) -> None:
        """Handle the thermostat's records."""
        if command_id != CMD_RECORD or not payload:
            return
        if payload[0] == RECORD_STATUS:
            self._parse_status(payload)
            self.resync_clock_if_due()
            if self._settings is None and not self._initial_settings_requested:
                self._initial_settings_requested = True
                self.create_catching_task(self._request_settings_quietly())
        elif payload[0] == RECORD_SETTINGS:
            self._parse_settings(payload)

    async def _request_settings_quietly(self) -> None:
        with contextlib.suppress(Exception):
            await self.b_command("B74", bytes(5))

    def _parse_status(self, record: bytes) -> None:
        if len(record) < STATUS_MIN_LENGTH:
            return
        hold_type = record[ST_HOLD_TYPE]
        mode = record[ST_SYSTEM_MODE]
        thermostat = self._thermostat
        attrs = Thermostat.AttributeDefs

        if int.from_bytes(record[ST_TEMPERATURE : ST_TEMPERATURE + 2], "big") != (
            PLACEHOLDER_TEMPERATURE
        ):
            thermostat.update_attribute(
                attrs.local_temperature.id, bcd_x100(record, ST_TEMPERATURE)
            )
        if hold_type == HoldType.standby:
            system_mode = SystemMode.Off
        elif mode == SYSTEM_MODE_COOL:
            system_mode = SystemMode.Cool
        else:
            system_mode = SystemMode.Heat
        thermostat.update_attribute(attrs.system_mode.id, system_mode)
        setpoint = bcd_x100(record, ST_SETPOINT)
        if hold_type != HoldType.standby:  # in standby bytes 9-10 hold the frost setpoint
            if mode == SYSTEM_MODE_COOL:
                thermostat.update_attribute(attrs.occupied_cooling_setpoint.id, setpoint)
            else:
                thermostat.update_attribute(attrs.occupied_heating_setpoint.id, setpoint)
        thermostat.update_attribute(
            attrs.running_state.id,
            RunningState.Heat_State_On if record[ST_RUNNING_STATE] else RunningState.Idle,
        )

        self.update_attribute(self.AttributeDefs.system_mode_raw.id, mode)
        with contextlib.suppress(ValueError):
            self.update_attribute(self.AttributeDefs.hold_type.id, HoldType(hold_type))
        self.update_attribute(
            self.AttributeDefs.schedule_setpoint.id,
            bcd_x100(record, ST_SCHEDULE_SETPOINT),
        )
        self.update_attribute(
            self.AttributeDefs.child_lock.id, t.Bool(record[ST_KEY_LOCK] == 1)
        )

        humidity = self.endpoint.in_clusters.get(RelativeHumidity.cluster_id)
        if humidity is not None:
            humidity.update_attribute(
                RelativeHumidity.AttributeDefs.measured_value.id,
                _bcd(record[ST_HUMIDITY]) * 100,
            )
        battery = self.endpoint.in_clusters.get(PowerConfiguration.cluster_id)
        if isinstance(battery, SalusBattery) and record[ST_BATTERY] < len(BATTERY_PERCENT):
            battery.update_attribute(
                PowerConfiguration.AttributeDefs.battery_percentage_remaining.id,
                BATTERY_PERCENT[record[ST_BATTERY]] * 2,
            )

    def _parse_settings(self, record: bytes) -> None:
        if len(record) < SETTINGS_LENGTH:
            return
        self._settings = bytes(record)
        if self._settings_waiter is not None and not self._settings_waiter.done():
            self._settings_waiter.set_result(bytes(record))
        attrs = self.AttributeDefs
        self.update_attribute(attrs.frost_setpoint.id, bcd_x100(record, SE_FROST_SETPOINT))
        self.update_attribute(
            attrs.temperature_offset.id, signed_bcd_x100(record, SE_TEMPERATURE_OFFSET)
        )
        with contextlib.suppress(ValueError):
            self.update_attribute(attrs.heat_control.id, HeatControl(record[SE_HEAT_CONTROL]))
        with contextlib.suppress(ValueError):
            self.update_attribute(
                attrs.display_resolution.id,
                DisplayResolution(record[SE_DISPLAY_RESOLUTION]),
            )
        min_heat = bcd_x100(record, SE_MIN_HEAT_SETPOINT)
        max_heat = bcd_x100(record, SE_MAX_HEAT_SETPOINT)
        tattrs = Thermostat.AttributeDefs
        self._thermostat.update_attribute(tattrs.min_heat_setpoint_limit.id, min_heat)
        self._thermostat.update_attribute(tattrs.max_heat_setpoint_limit.id, max_heat)
        self._thermostat.update_attribute(
            tattrs.min_cool_setpoint_limit.id, bcd_x100(record, SE_MIN_COOL_SETPOINT)
        )
        self._thermostat.update_attribute(
            tattrs.max_cool_setpoint_limit.id, bcd_x100(record, SE_MAX_COOL_SETPOINT)
        )

    # --- writes ----------------------------------------------------------------

    async def set_setpoint(self, hundredths: int) -> None:
        """B34 in heat mode, B33 in cool mode; the thermostat ignores the other one."""
        cool = self.get(self.AttributeDefs.system_mode_raw.id) == SYSTEM_MODE_COOL
        tattrs = Thermostat.AttributeDefs
        if cool:
            low = self._thermostat.get(tattrs.min_cool_setpoint_limit.id, 500)
            high = self._thermostat.get(tattrs.max_cool_setpoint_limit.id, 4000)
        else:
            low = self._thermostat.get(tattrs.min_heat_setpoint_limit.id, 500)
            high = self._thermostat.get(tattrs.max_heat_setpoint_limit.id, 3500)
        hundredths = round(hundredths / 50) * 50
        if not low <= hundredths <= high:
            raise SalusError(f"Setpoint must be between {low / 100} and {high / 100}")
        async with self._lock:
            await self.b_command("B33" if cool else "B34", bytes([setpoint_byte(hundredths)]))
        self._thermostat.update_attribute(
            tattrs.occupied_cooling_setpoint.id if cool else tattrs.occupied_heating_setpoint.id,
            hundredths,
        )

    async def set_system_mode(self, mode: SystemMode) -> None:
        """Off is hold type standby; on is permanent hold followed by the system mode."""
        async with self._lock:
            if mode == SystemMode.Off:
                await self.b_command("B20", bytes([HoldType.standby]))
                self.update_attribute(self.AttributeDefs.hold_type.id, HoldType.standby)
            else:
                mode_byte = SYSTEM_MODE_COOL if mode == SystemMode.Cool else SYSTEM_MODE_HEAT
                await self.b_command("B20", bytes([HoldType.permanent_hold]))
                await self.b_command("B48", bytes([mode_byte]))
                self.update_attribute(self.AttributeDefs.hold_type.id, HoldType.permanent_hold)
                self.update_attribute(self.AttributeDefs.system_mode_raw.id, mode_byte)
        self._thermostat.update_attribute(Thermostat.AttributeDefs.system_mode.id, mode)

    async def set_hold_type(self, hold_type: HoldType) -> None:
        """B20 with the hold type."""
        hold_type = HoldType(hold_type)
        if hold_type not in SETTABLE_HOLD_TYPES | {HoldType.standby}:
            raise SalusError(
                "Hold type can be set to schedule, permanent_hold, away or standby"
            )
        async with self._lock:
            await self.b_command("B20", bytes([hold_type]))
        self.update_attribute(self.AttributeDefs.hold_type.id, hold_type)
        if hold_type == HoldType.standby:
            system_mode = SystemMode.Off
        elif self.get(self.AttributeDefs.system_mode_raw.id) == SYSTEM_MODE_COOL:
            system_mode = SystemMode.Cool
        else:
            system_mode = SystemMode.Heat
        self._thermostat.update_attribute(Thermostat.AttributeDefs.system_mode.id, system_mode)

    async def set_child_lock(self, locked: bool) -> None:
        """B03 with arg2 'A' (lock) or 'B' (unlock)."""
        async with self._lock:
            await self.b_command("B03", bytes([0x00, 0x00, 0x41 if locked else 0x42]) + bytes(10))
        self.update_attribute(self.AttributeDefs.child_lock.id, t.Bool(locked))

    async def _read_settings(self) -> bytes:
        """Ask for a fresh 0x72 with B74 and return it."""
        self._settings_waiter = asyncio.get_running_loop().create_future()
        try:
            await self.b_command("B74", bytes(5))
            return await asyncio.wait_for(self._settings_waiter, SETTINGS_TIMEOUT)
        except TimeoutError as exc:
            raise SalusError("No settings record (0x72) after B74") from exc
        finally:
            self._settings_waiter = None

    async def write_settings(self, kind: int, change) -> None:
        """Read a fresh 0x72, change it, write it back with B11, then read it again."""
        async with self._lock:
            record = bytearray(await self._read_settings())
            if pin_of(record) != NO_PIN:
                raise SalusError(
                    "The thermostat has a PIN set; its settings are not written, as a "
                    "wrong PIN would lock its keys"
                )
            change(record)
            record[0] = kind
            record[SE_COUNTER], record[SE_PIN], record[SE_PIN + 1] = NO_PIN_BYTES
            await self.b_command("B11", bytes(record[:SETTINGS_LENGTH]))
            await self._read_settings()

    async def refresh_settings(self) -> None:
        """Ask the thermostat for its settings record."""
        await self._read_settings()

    async def pair_wiring_centre_zones(self, zones: str, replace: bool = False) -> None:
        """Pair this thermostat with zones of the wiring centre (PROTOCOL 7.1, 7.7).

        `zones` is zone numbers 1-8 separated by commas, e.g. "1,4". With `replace`, a
        zone another thermostat (or an old address of this one) holds is taken over.
        """
        numbers = sorted({int(z) for z in str(zones).replace(" ", ",").split(",") if z})
        if not numbers or any(not 1 <= z <= ZONES for z in numbers):
            raise SalusError(f"Zones must be numbers 1 to 8, e.g. '1,4'; got {zones!r}")
        wiring_centre = find_wiring_centre(self.endpoint.device.application)
        if wiring_centre is None:
            raise SalusError("No KL08RF wiring centre has joined this network")

        args = bytearray(B47_LENGTH)
        args[0:2] = b"\x00\x11"
        args[2:4] = WIRING_CENTRE_NUMBER.to_bytes(2, "little")
        args[4:6] = int(wiring_centre.nwk).to_bytes(2, "little")
        args[6] = 0xFE
        args[7] = 0x01 if replace else 0x00
        for zone in numbers:
            args[7 + zone] = 0x01

        future = asyncio.get_running_loop().create_future()
        pairing = (int(self.endpoint.device.nwk), tuple(numbers), future)
        async with self._lock:
            if replace:
                reset = bytearray(B47_LENGTH)
                reset[0:7] = b"\x00\x11\xff\xff\xff\xff\xfd"
                await self.b_command("B47", bytes(reset))
            _zone_pairings.add(pairing)
            try:
                await self.b_command("B47", bytes(args))
                timeout = ZONE_PAIRING_TIMEOUT_PER_ZONE * len(numbers)
                try:
                    await asyncio.wait_for(future, timeout)
                except TimeoutError as exc:
                    raise SalusError(
                        f"The wiring centre's zone table did not show this thermostat in "
                        f"zone(s) {numbers} within {timeout} s. A zone that another "
                        "thermostat holds needs replace: true"
                    ) from exc
            finally:
                _zone_pairings.discard(pairing)

    async def write_attributes(self, attributes, manufacturer=None, **kwargs):
        """Write the settings entities with B-commands."""
        attrs = self.AttributeDefs
        for key, value in attributes.items():
            attr = self.find_attribute(key)
            if attr == attrs.hold_type:
                await self.set_hold_type(value)
            elif attr == attrs.child_lock:
                await self.set_child_lock(bool(value))
            elif attr == attrs.frost_setpoint:
                hundredths = round(int(value) / 50) * 50
                if not 500 <= hundredths <= 3500:
                    raise SalusError("Frost setpoint must be between 5 and 35")
                await self.write_settings(
                    B11_USER,
                    lambda r, v=hundredths: r.__setitem__(
                        slice(SE_FROST_SETPOINT, SE_FROST_SETPOINT + 2), bcd_x100_bytes(v)
                    ),
                )
            elif attr == attrs.temperature_offset:
                hundredths = round(int(value) / 10) * 10
                if not -350 <= hundredths <= 350:
                    raise SalusError("Temperature offset must be between -3.5 and 3.5")
                await self.write_settings(
                    B11_USER,
                    lambda r, v=hundredths: r.__setitem__(
                        slice(SE_TEMPERATURE_OFFSET, SE_TEMPERATURE_OFFSET + 2),
                        signed_bcd_x100_bytes(v),
                    ),
                )
            elif attr == attrs.heat_control:
                await self.write_settings(
                    B11_ADMIN,
                    lambda r, v=HeatControl(value): r.__setitem__(SE_HEAT_CONTROL, v),
                )
            elif attr == attrs.display_resolution:
                await self.write_settings(
                    B11_ADMIN,
                    lambda r, v=DisplayResolution(value): r.__setitem__(
                        SE_DISPLAY_RESOLUTION, v
                    ),
                )
            else:
                raise SalusError(f"{attr.name} cannot be written")
        return [[foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS)]]

    async def write_heat_limit(self, is_min: bool, value: int) -> None:
        """Write the minimum or maximum heating setpoint (0x72 bytes 24-25 / 22-23)."""
        hundredths = round(value / 50) * 50

        def change(record: bytearray) -> None:
            low = bcd_x100(record, SE_MIN_HEAT_SETPOINT)
            high = bcd_x100(record, SE_MAX_HEAT_SETPOINT)
            if is_min and not 500 <= hundredths <= high - 50:
                raise SalusError(f"Minimum heating setpoint must be 5 to {(high - 50) / 100}")
            if not is_min and not low + 50 <= hundredths <= 4000:
                raise SalusError(f"Maximum heating setpoint must be {(low + 50) / 100} to 40")
            offset = SE_MIN_HEAT_SETPOINT if is_min else SE_MAX_HEAT_SETPOINT
            record[offset : offset + 2] = bcd_x100_bytes(hundredths)

        await self.write_settings(B11_ADMIN, change)


class SalusWiringCentreCluster(SalusBaseCluster):
    """FC00 cluster of a KL08RF / it600WC wiring centre."""

    device_number = WIRING_CENTRE_NUMBER

    class AttributeDefs(BaseAttributeDefs):
        """The zone table: IEEE address of the thermostat in each zone."""

        zone_1 = ZCLAttributeDef(id=0x0001, type=t.CharacterString, access="r")
        zone_2 = ZCLAttributeDef(id=0x0002, type=t.CharacterString, access="r")
        zone_3 = ZCLAttributeDef(id=0x0003, type=t.CharacterString, access="r")
        zone_4 = ZCLAttributeDef(id=0x0004, type=t.CharacterString, access="r")
        zone_5 = ZCLAttributeDef(id=0x0005, type=t.CharacterString, access="r")
        zone_6 = ZCLAttributeDef(id=0x0006, type=t.CharacterString, access="r")
        zone_7 = ZCLAttributeDef(id=0x0007, type=t.CharacterString, access="r")
        zone_8 = ZCLAttributeDef(id=0x0008, type=t.CharacterString, access="r")
        replaced_thermostat = ZCLAttributeDef(id=0x0009, type=t.CharacterString, access="r")

    _VALID_ATTRIBUTES = {attr.id for attr in AttributeDefs}

    class ServerCommandDefs(BaseCommandDefs):
        """Actions for Developer tools -> zha.issue_zigbee_cluster_command (not sent as is)."""

        request_zone_table = ZCLCommandDef(id=0xE0, schema={})
        send_b_command = ZCLCommandDef(id=0xE1, schema={"hex_payload": t.CharacterString})

    def __init__(self, *args, **kwargs):
        """Init."""
        super().__init__(*args, **kwargs)
        self._zone_table_requested = False

    async def send_sync(self) -> None:
        """Greet, then ask for the zone table once."""
        await super().send_sync()
        if not self._zone_table_requested:
            self._zone_table_requested = True
            with contextlib.suppress(Exception):
                await self.request_zone_table()

    async def request_zone_table(self) -> None:
        """Ask for the B70 zone table; it comes back as a 0x33 report."""
        await self.b_command("B70", ZONE_TABLE_REQUEST[3:], wait=False)

    def handle_echo(self, payload: bytes) -> None:
        """Publish the B70 zone table."""
        if payload[1:4] != b"B70" or len(payload) < ZONE_TABLE_LENGTH:
            return
        application = self.endpoint.device.application
        addresses = []
        for zone in range(ZONES):
            offset = ZONE_TABLE_FIRST_SLOT + ZONE_TABLE_SLOT_LENGTH * zone
            nwk = int.from_bytes(payload[offset : offset + 2], "big")
            addresses.append(None if nwk == FREE_ZONE else nwk)
            self.update_attribute(zone + 1, _address_name(application, addresses[-1]))
        for nwk, zones, future in list(_zone_pairings):
            if all(addresses[z - 1] == nwk for z in zones) and not future.done():
                future.set_result(None)

    def handle_salus_frame(self, command_id: int, payload: bytes) -> None:
        """Log the wiring centre's other frames."""
        if command_id == CMD_REPLACED_THERMOSTAT and len(payload) >= 4:
            nwk = int.from_bytes(payload[2:4], "little")
            self.update_attribute(
                self.AttributeDefs.replaced_thermostat.id,
                _address_name(self.endpoint.device.application, nwk),
            )
        elif command_id == CMD_SYNC_ACK:
            self.debug("Wiring centre accepted the sync response: %s", payload.hex(" "))
        else:
            self.debug("Wiring centre frame 0x%02x: %s", command_id, payload.hex(" "))

    async def write_attributes(self, attributes, manufacturer=None, **kwargs):
        """The zone table is read only."""
        raise SalusError("The zone table is read only; pair zones from the thermostat")


class SalusWiringCentreClientCluster(LocalDataCluster):
    """The wiring centre sends FC00 frames with the client-to-server bit (0x15).

    zigpy delivers those to the client cluster, which hands them to the server one.
    """

    cluster_id = SALUS_CLUSTER_ID
    ep_attribute = "salus_it600_client"

    def deserialize(self, data: bytes):
        """Return cluster-specific frames as raw payload bytes."""
        hdr, rest = foundation.ZCLHeader.deserialize(data)
        if hdr.frame_control.frame_type == foundation.FrameType.CLUSTER_COMMAND:
            return hdr, bytes(rest)
        return super().deserialize(data)

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        """Forward to the server cluster."""
        server = self.endpoint.in_clusters.get(SALUS_CLUSTER_ID)
        if isinstance(server, SalusBaseCluster):
            server.handle_cluster_request(hdr, args)


class SalusThermostat(LocalDataCluster, Thermostat):
    """Thermostat cluster held locally, filled from 0x71 records, written with B-commands."""

    _CONSTANT_ATTRIBUTES = {
        Thermostat.AttributeDefs.ctrl_sequence_of_oper.id: (
            ControlSequenceOfOperation.Heating_Only
        ),
        Thermostat.AttributeDefs.abs_min_heat_setpoint_limit.id: 500,
        Thermostat.AttributeDefs.abs_max_heat_setpoint_limit.id: 4000,
    }
    _DEFAULT_VALUES = {
        Thermostat.AttributeDefs.min_heat_setpoint_limit.id: 500,
        Thermostat.AttributeDefs.max_heat_setpoint_limit.id: 3500,
    }
    # Filled from the records later; ZHA must not mark them unsupported at startup.
    _VALID_ATTRIBUTES = {
        Thermostat.AttributeDefs.local_temperature.id,
        Thermostat.AttributeDefs.occupied_heating_setpoint.id,
        Thermostat.AttributeDefs.occupied_cooling_setpoint.id,
        Thermostat.AttributeDefs.system_mode.id,
        Thermostat.AttributeDefs.running_state.id,
        Thermostat.AttributeDefs.min_heat_setpoint_limit.id,
        Thermostat.AttributeDefs.max_heat_setpoint_limit.id,
        Thermostat.AttributeDefs.min_cool_setpoint_limit.id,
        Thermostat.AttributeDefs.max_cool_setpoint_limit.id,
    }

    @property
    def _salus(self) -> SalusThermostatCluster:
        return self.endpoint.in_clusters[SALUS_CLUSTER_ID]

    async def write_attributes(self, attributes, manufacturer=None, **kwargs):
        """Translate climate entity writes into B-commands."""
        attrs = Thermostat.AttributeDefs
        for key, value in attributes.items():
            attr = self.find_attribute(key)
            if attr in (attrs.occupied_heating_setpoint, attrs.occupied_cooling_setpoint):
                await self._salus.set_setpoint(int(value))
            elif attr == attrs.system_mode:
                await self._salus.set_system_mode(SystemMode(value))
            elif attr in (attrs.min_heat_setpoint_limit, attrs.max_heat_setpoint_limit):
                await self._salus.write_heat_limit(
                    attr == attrs.min_heat_setpoint_limit, int(value)
                )
            else:
                raise SalusError(f"{attr.name} cannot be written on this thermostat")
        return [[foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS)]]


class SalusHumidity(LocalDataCluster, RelativeHumidity):
    """Humidity from the 0x71 record."""

    _VALID_ATTRIBUTES = {RelativeHumidity.AttributeDefs.measured_value.id}


class SalusBattery(LocalDataCluster, PowerConfiguration):
    """Battery level from the 0x71 record (battery model only)."""

    _VALID_ATTRIBUTES = {PowerConfiguration.AttributeDefs.battery_percentage_remaining.id}


class SalusGatewayBasic(LocalDataCluster, Basic):
    """Answers the thermostat's Basic reads of the coordinator as a Salus gateway would.

    The thermostat reads the coordinator's modelId during its greeting and gets
    'SAU2AG1' from the Salus gateway (PROTOCOL 3). Whether it needs that is unknown.
    """

    ep_attribute = "salus_gateway_basic"

    def handle_cluster_general_request(self, hdr, args, *, dst_addressing=None):
        """Reply to Read Attributes from coordinator endpoint 1."""
        if hdr.command_id != foundation.GeneralCommand.Read_Attributes:
            return
        values = {
            Basic.AttributeDefs.manufacturer.id: "Computime",
            Basic.AttributeDefs.model.id: "SAU2AG1",
        }
        records = []
        for attrid in args.attribute_ids:
            if attrid in values:
                records.append(
                    foundation.ReadAttributeRecord(
                        attrid=attrid,
                        status=foundation.Status.SUCCESS,
                        value=foundation.TypeValue(
                            type=foundation.DataTypeId.string,
                            value=t.CharacterString(values[attrid]),
                        ),
                    )
                )
            else:
                records.append(
                    foundation.ReadAttributeRecord(
                        attrid=attrid, status=foundation.Status.UNSUPPORTED_ATTRIBUTE
                    )
                )
        self.create_catching_task(self._reply(hdr.tsn, records))

    async def _reply(self, tsn: int, records) -> None:
        command = foundation.GENERAL_COMMANDS[
            foundation.GeneralCommand.Read_Attributes_rsp
        ]
        hdr = foundation.ZCLHeader.general(
            tsn=tsn,
            command_id=foundation.GeneralCommand.Read_Attributes_rsp,
            direction=foundation.Direction.Server_to_Client,
        )
        await self.endpoint.device.request(
            profile=zha_profile.PROFILE_ID,
            cluster=Basic.cluster_id,
            src_ep=COORDINATOR_ENDPOINT,
            dst_ep=self.endpoint.endpoint_id,
            sequence=tsn,
            data=hdr.serialize() + command.schema(status_records=records).serialize(),
            expect_reply=False,
        )


def _has_salus_endpoint(endpoint_id: int):
    def check(device) -> bool:
        endpoint = device.endpoints.get(endpoint_id)
        return (
            endpoint is not None
            and not isinstance(endpoint, int)
            and SALUS_CLUSTER_ID in endpoint.in_clusters
        )

    return check


def _thermostat_quirk(models: tuple[str, ...], battery: bool) -> None:
    builder = QuirkBuilder()
    for model in models:
        # Releases before zha-quirks 2.0 match the exact (manufacturer, model) pair, so
        # the likely manufacturer names are listed; None is a wildcard from 2.0 on.
        for manufacturer in (*MANUFACTURER_NAMES, None):
            builder.applies_to(manufacturer, model)
    ep = THERMOSTAT_ENDPOINT
    fc00 = SalusThermostatCluster
    attrs = fc00.AttributeDefs
    builder = (
        builder.filter(_has_salus_endpoint(ep))
        .replaces(fc00, endpoint_id=ep)
        .adds(SalusThermostat, endpoint_id=ep)
        .adds(SalusHumidity, endpoint_id=ep)
        .adds(SalusGatewayBasic, cluster_type=ClusterType.Client, endpoint_id=ep)
    )
    if battery:
        builder = builder.adds(SalusBattery, endpoint_id=ep)
    (
        builder.enum(
            attrs.hold_type.name,
            HoldType,
            fc00.cluster_id,
            endpoint_id=ep,
            entity_type=EntityType.STANDARD,
            translation_key="salus_hold_type",
            fallback_name="Hold type",
        )
        .switch(
            attrs.child_lock.name,
            fc00.cluster_id,
            endpoint_id=ep,
            translation_key="child_lock",
            fallback_name="Child lock",
        )
        .sensor(
            attrs.schedule_setpoint.name,
            fc00.cluster_id,
            endpoint_id=ep,
            divisor=100,
            device_class=SensorDeviceClass.TEMPERATURE,
            unit=UnitOfTemperature.CELSIUS,
            translation_key="salus_schedule_setpoint",
            fallback_name="Schedule setpoint",
        )
        .number(
            attrs.frost_setpoint.name,
            fc00.cluster_id,
            endpoint_id=ep,
            min_value=5,
            max_value=35,
            step=0.5,
            multiplier=0.01,
            unit=UnitOfTemperature.CELSIUS,
            translation_key="salus_frost_setpoint",
            fallback_name="Frost setpoint",
        )
        .number(
            attrs.temperature_offset.name,
            fc00.cluster_id,
            endpoint_id=ep,
            min_value=-3.5,
            max_value=3.5,
            step=0.1,
            multiplier=0.01,
            unit=UnitOfTemperature.CELSIUS,
            translation_key="local_temperature_calibration",
            fallback_name="Temperature offset",
        )
        .enum(
            attrs.heat_control.name,
            HeatControl,
            fc00.cluster_id,
            endpoint_id=ep,
            translation_key="salus_heat_control",
            fallback_name="Heat control algorithm",
        )
        .enum(
            attrs.display_resolution.name,
            DisplayResolution,
            fc00.cluster_id,
            endpoint_id=ep,
            translation_key="salus_display_resolution",
            fallback_name="Display resolution",
        )
        # ZHA's own min/max heat setpoint limit numbers on the Thermostat cluster
        # write the limits; SalusThermostat sends them with B11.
        .add_to_registry()
    )


_thermostat_quirk(tuple(m for m in THERMOSTAT_MODELS if m not in BATTERY_MODELS), False)
_thermostat_quirk(BATTERY_MODELS, True)


def _wiring_centre_quirk() -> None:
    ep = WIRING_CENTRE_ENDPOINT
    fc00 = SalusWiringCentreCluster
    # The wiring centre reports no modelId, so it is matched by manufacturer and its
    # endpoint 8 with the Salus cluster (the SP600 plugs use endpoint 9 and 0xFC01).
    builder = (
        QuirkBuilder()
        .applies_to("Computime", None)
        .filter(_has_salus_endpoint(ep))
        .replaces(fc00, endpoint_id=ep)
        .adds(SalusWiringCentreClientCluster, cluster_type=ClusterType.Client, endpoint_id=ep)
    )
    for zone in range(1, ZONES + 1):
        builder = builder.sensor(
            f"zone_{zone}",
            fc00.cluster_id,
            endpoint_id=ep,
            entity_type=EntityType.DIAGNOSTIC,
            translation_key=f"salus_zone_{zone}",
            fallback_name=f"Zone {zone} thermostat",
        )
    builder.sensor(
        fc00.AttributeDefs.replaced_thermostat.name,
        fc00.cluster_id,
        endpoint_id=ep,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="salus_replaced_thermostat",
        fallback_name="Replaced thermostat",
        initially_disabled=True,
    ).add_to_registry()


_wiring_centre_quirk()

_LOGGER.warning("Salus iT600 quirk loaded (%s)", _VERSIONS)

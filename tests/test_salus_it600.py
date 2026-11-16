"""Tests for the Salus iT600 ZHA quirk against simulated devices.

Copyright (c) 2026 Maciej Kornacki. SPDX-License-Identifier: MIT (see LICENSE).
Test vectors (factory settings record, sync response example) are taken from
PROTOCOL.md in https://github.com/mikkelke/salus-zigbee by Mikkel Eskildsen (MIT).

Run with zha-quirks installed:  pytest tests/
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import pathlib
import sys
from unittest import mock

import pytest
import zigpy.application
import zigpy.device
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import Basic, PowerConfiguration
from zigpy.zcl.clusters.hvac import RunningState, SystemMode, Thermostat
from zigpy.zcl.clusters.measurement import RelativeHumidity
from zigpy.zdo.types import NodeDescriptor
import zigpy.types as t

try:  # zha-quirks 2.x
    from zha.quirks import DEVICE_REGISTRY

    resolve_quirk = DEVICE_REGISTRY.resolve
except ImportError:  # older releases keep the registry in zigpy
    from zigpy.quirks import get_device as resolve_quirk

QUIRK_PATH = pathlib.Path(__file__).parent.parent / "salus_it600.py"
spec = importlib.util.spec_from_file_location("salus_it600", QUIRK_PATH)
salus = importlib.util.module_from_spec(spec)
sys.modules["salus_it600"] = salus
spec.loader.exec_module(salus)

THERMOSTAT_IEEE = t.EUI64.convert("00:1e:5e:00:00:00:00:01")
WC_IEEE = t.EUI64.convert("00:1e:5e:00:00:00:00:02")
COORDINATOR_IEEE = t.EUI64.convert("00:12:4b:00:00:00:00:00")

FACTORY_SETTINGS = bytes.fromhex(
    "72 07 01 00 00 01 05 00 40 50 00 00 00 01 04 00 27 00 10 00 06 00 35 00 05 00 40 00 05 00"
    "01 00 00 00 01 01 80 00 00 00 0d fe 00 00 00 00 01 00"
) + b"\xff" * 27 + b"\x00\x00"


def status_record(
    temperature=b"\x22\x90",
    setpoint=b"\x21\x00",
    hold=2,
    mode=4,
    running=0,
    lock=0,
    humidity=0x53,
    battery=5,
) -> bytes:
    record = bytearray(77)
    record[0] = 0x71
    record[1] = 16
    record[2] = 0x0D
    record[3] = hold
    record[4:6] = temperature
    record[9:11] = setpoint
    record[11:13] = b"\x19\x00"
    record[13:15] = b"\x24\x00"
    record[15] = mode
    record[17] = running
    record[23] = lock
    record[24] = humidity
    record[43:45] = b"\x05\x00"
    record[49] = battery
    return bytes(record)


class MockApp(zigpy.application.ControllerApplication):
    """App controller."""

    async def add_endpoint(self, descriptor):
        """Mock add_endpoint."""

    async def connect(self):
        """Mock."""

    async def disconnect(self):
        """Mock."""

    async def start_network(self):
        """Mock."""

    async def force_remove(self, dev):
        """Mock."""

    async def load_network_info(self, *, load_devices=False):
        """Mock."""

    async def permit_ncp(self, time_s=60):
        """Mock."""

    async def permit_with_link_key(self, node, link_key, time_s=60):
        """Mock."""

    async def reset_network_info(self):
        """Mock."""

    async def send_packet(self, packet):
        """Mock."""

    async def write_network_info(self, *, network_info, node_info):
        """Mock."""


@pytest.fixture
def app():
    return MockApp({"device": {"path": "/dev/null"}, "database": None})


def make_device(app, ieee, nwk, manufacturer, model, ep_id, in_clusters, out_clusters):
    raw = zigpy.device.Device(app, ieee, nwk)
    raw.manufacturer = manufacturer
    raw.model = model
    raw.node_desc = NodeDescriptor(manufacturer_code=0x1078)
    ep = raw.add_endpoint(ep_id)
    ep.profile_id = 0x0104
    ep.device_type = 0x0301
    for cluster_id in in_clusters:
        ep.add_input_cluster(cluster_id)
    for cluster_id in out_clusters:
        ep.add_output_cluster(cluster_id)
    quirked = resolve_quirk(raw)
    app.devices[ieee] = quirked
    return quirked


class SimulatedSalusDevice:
    """Plays the device side: echoes B-commands, answers B74 with a 0x72."""

    def __init__(self, device, ep_id, settings=FACTORY_SETTINGS, direction=None):
        self.device = device
        self.ep_id = ep_id
        self.settings = bytearray(settings)
        self.sent: list[tuple[int, int, int, bytes, bytes]] = []  # (src_ep, dst_ep, cmd, hdr, payload)
        self.direction = direction or foundation.Direction.Server_to_Client
        self.echo = True
        self.zone_table: bytes | None = None
        self.tsn = 0
        self.mock = mock.AsyncMock(side_effect=self._request)
        device.request = self.mock

    def receive(self, command_id, payload, cluster=salus.SALUS_CLUSTER_ID):
        self.tsn = (self.tsn + 1) % 256
        hdr = foundation.ZCLHeader.cluster(
            tsn=self.tsn, command_id=command_id, manufacturer=0x1078, direction=self.direction
        )
        self.device.packet_received(
            t.ZigbeePacket(
                profile_id=260,
                cluster_id=cluster,
                src_ep=self.ep_id,
                dst_ep=1,
                data=t.SerializableBytes(hdr.serialize() + payload),
            )
        )

    async def _request(self, *, profile, cluster, src_ep, dst_ep, sequence, data, expect_reply):
        hdr, payload = foundation.ZCLHeader.deserialize(data)
        self.sent.append((src_ep, dst_ep, hdr.command_id, data[: len(data) - len(payload)], payload))
        if cluster == salus.SALUS_CLUSTER_ID and hdr.command_id == salus.CMD_WRITE and self.echo:
            loop = asyncio.get_running_loop()
            loop.call_soon(self.receive, salus.CMD_ECHO, payload)
            code = payload[1:4]
            if code == b"B74":
                loop.call_soon(self.receive, salus.CMD_RECORD, bytes(self.settings))
            elif code == b"B11":
                self.settings[1:48] = payload[5:52]
            elif code == b"B47" and payload[10] == 0xFE and self.zone_table is not None:
                loop.call_later(0.01, self.receive, salus.CMD_ECHO, self.zone_table)

    def writes(self):
        return [p for (_, _, cmd, _, p) in self.sent if cmd == salus.CMD_WRITE]


@pytest.fixture
def thermostat(app):
    device = make_device(
        app, THERMOSTAT_IEEE, 0x1234, "Computime", "SQ610RF", 9,
        [0x0000, 0x0003, 0x000A, 0x0B05, 0xFC00], [0x0003, 0x0019, 0xFC00],
    )
    return SimulatedSalusDevice(device, 9)


def test_quirk_applies(thermostat):
    ep = thermostat.device.endpoints[9]
    assert isinstance(ep.in_clusters[0xFC00], salus.SalusThermostatCluster)
    assert isinstance(ep.in_clusters[Thermostat.cluster_id], salus.SalusThermostat)
    assert isinstance(ep.in_clusters[RelativeHumidity.cluster_id], salus.SalusHumidity)
    assert isinstance(ep.out_clusters[Basic.cluster_id], salus.SalusGatewayBasic)
    assert PowerConfiguration.cluster_id not in ep.in_clusters  # mains powered


async def test_battery_model_gets_battery(app):
    device = make_device(
        app, THERMOSTAT_IEEE, 0x1234, "Computime", "SQ610RFNH", 9,
        [0x0000, 0x0003, 0xFC00], [0x0019, 0xFC00],
    )
    assert isinstance(device.endpoints[9].in_clusters[PowerConfiguration.cluster_id], salus.SalusBattery)
    sim = SimulatedSalusDevice(device, 9)
    sim.receive(salus.CMD_RECORD, status_record(battery=4))
    battery = device.endpoints[9].in_clusters[PowerConfiguration.cluster_id]
    assert battery.get("battery_percentage_remaining") == 150


def test_other_computime_device_untouched(app):
    device = make_device(
        app, THERMOSTAT_IEEE, 0x1234, "Computime", "SQ610RF", 1, [0x0000, 0x0006], []
    )
    assert not isinstance(device.endpoints[1].in_clusters[0x0006], salus.SalusBaseCluster)


async def test_greeting_answered_from_coordinator_ep1(thermostat):
    thermostat.receive(salus.CMD_SYNC_REQUEST, bytes.fromhex("ff42ff50000000"))
    await asyncio.sleep(0.05)
    syncs = [s for s in thermostat.sent if s[2] == salus.CMD_SYNC_RESPONSE]
    assert syncs, thermostat.sent
    src_ep, dst_ep, _, hdr, payload = syncs[-1]
    assert (src_ep, dst_ep) == (1, 9)
    assert hdr[0] == 0x1D and hdr[1:3] == b"\x78\x10"
    assert len(payload) == 15 and payload[0] == 0x20 and payload[9:14] == b"\xff\xff\xff\xff\x31"


def test_sync_payload_copenhagen_summer():
    from zoneinfo import ZoneInfo

    with mock.patch.object(salus, "_local_zone", return_value=ZoneInfo("Europe/Copenhagen")):
        now = dt.datetime(2026, 10, 15, 18, 44, 26, tzinfo=dt.UTC)  # 20:44:26 CEST
        payload = salus.sync_response_payload(0x20, now)
    # The example response of PROTOCOL 4.2 for this time.
    assert payload.hex(" ") == "20 00 aa f7 63 32 03 0e 01 ff ff ff ff 31 00"
    with mock.patch.object(salus, "_local_zone", return_value=ZoneInfo("Europe/Warsaw")):
        winter = salus.sync_response_payload(1, dt.datetime(2026, 1, 15, 12, tzinfo=dt.UTC))
    assert winter[0] == 1 and winter[6] == 0x02 and winter[7] == 14 and winter[8] == 1


async def test_status_record_maps_to_thermostat(thermostat):
    thermostat.receive(salus.CMD_RECORD, status_record(running=1, lock=1))
    ep = thermostat.device.endpoints[9]
    thermo = ep.in_clusters[Thermostat.cluster_id]
    assert thermo.get("local_temperature") == 2290
    assert thermo.get("occupied_heating_setpoint") == 2100
    assert thermo.get("system_mode") == SystemMode.Heat
    assert thermo.get("running_state") == RunningState.Heat_State_On
    assert thermo.get("ctrl_sequence_of_oper") == 0x02
    assert ep.in_clusters[RelativeHumidity.cluster_id].get("measured_value") == 5300
    fc00 = ep.in_clusters[0xFC00]
    assert fc00.get("hold_type") == salus.HoldType.permanent_hold
    assert fc00.get("child_lock") is True or fc00.get("child_lock") == 1
    assert fc00.get("schedule_setpoint") == 1900

    # standby: off, setpoint unchanged; placeholder temperature ignored
    thermostat.receive(
        salus.CMD_RECORD, status_record(temperature=b"\x12\x34", setpoint=b"\x05\x00", hold=7)
    )
    assert thermo.get("system_mode") == SystemMode.Off
    assert thermo.get("local_temperature") == 2290
    assert thermo.get("occupied_heating_setpoint") == 2100
    await asyncio.sleep(0.05)
    # the first status record also resyncs the clock and asks for the settings
    assert any(s[2] == salus.CMD_SYNC_RESPONSE for s in thermostat.sent)
    assert fc00.get("frost_setpoint") == 500


async def test_settings_record(thermostat):
    thermostat.receive(salus.CMD_RECORD, FACTORY_SETTINGS)
    ep = thermostat.device.endpoints[9]
    fc00 = ep.in_clusters[0xFC00]
    assert fc00.get("frost_setpoint") == 500
    assert fc00.get("temperature_offset") == 0
    assert fc00.get("heat_control") == salus.HeatControl.itlc_underfloor
    assert fc00.get("display_resolution") == salus.DisplayResolution.half_degree
    thermo = ep.in_clusters[Thermostat.cluster_id]
    assert thermo.get("min_heat_setpoint_limit") == 500
    assert thermo.get("max_heat_setpoint_limit") == 3500
    assert salus.pin_of(FACTORY_SETTINGS) == salus.NO_PIN


async def test_setpoint_write(thermostat):
    thermostat.receive(salus.CMD_RECORD, status_record())
    thermo = thermostat.device.endpoints[9].in_clusters[Thermostat.cluster_id]
    result = await thermo.write_attributes({"occupied_heating_setpoint": 2150})
    assert result[0][0].status == foundation.Status.SUCCESS
    b34 = [w for w in thermostat.writes() if w[1:4] == b"B34"]
    assert b34 and b34[-1][4] == 0x4B  # 21.5 °C
    assert thermo.get("occupied_heating_setpoint") == 2150
    with pytest.raises(salus.SalusError):
        await thermo.write_attributes({"occupied_heating_setpoint": 3600})


async def test_cool_mode_uses_b33(thermostat):
    thermostat.receive(salus.CMD_RECORD, status_record(mode=3))
    thermo = thermostat.device.endpoints[9].in_clusters[Thermostat.cluster_id]
    await thermo.write_attributes({"occupied_cooling_setpoint": 3000})
    assert b"B33\x5c" in [w[1:5] for w in thermostat.writes()]


async def test_system_mode_writes(thermostat):
    thermo = thermostat.device.endpoints[9].in_clusters[Thermostat.cluster_id]
    await thermo.write_attributes({"system_mode": SystemMode.Off})
    assert thermostat.writes()[-1][1:5] == b"B20\x07"
    assert thermo.get("system_mode") == SystemMode.Off
    await thermo.write_attributes({"system_mode": SystemMode.Heat})
    assert [w[1:5] for w in thermostat.writes()[-2:]] == [b"B20\x02", b"B48\x04"]
    assert thermo.get("system_mode") == SystemMode.Heat


async def test_hold_type_and_lock(thermostat):
    fc00 = thermostat.device.endpoints[9].in_clusters[0xFC00]
    await fc00.write_attributes({"hold_type": salus.HoldType.schedule})
    assert thermostat.writes()[-1][1:5] == b"B20\x00"
    await fc00.write_attributes({"hold_type": salus.HoldType.away})
    assert thermostat.writes()[-1][1:5] == b"B20\x06"
    with pytest.raises(salus.SalusError):
        await fc00.write_attributes({"hold_type": salus.HoldType.eco})
    await fc00.write_attributes({"child_lock": 1})
    assert thermostat.writes()[-1][1:] == b"B03\x00\x00\x41" + bytes(10)
    await fc00.write_attributes({"child_lock": 0})
    assert thermostat.writes()[-1][1:7] == b"B03\x00\x00\x42"


async def test_frost_setpoint_settings_write(thermostat):
    fc00 = thermostat.device.endpoints[9].in_clusters[0xFC00]
    await fc00.write_attributes({"frost_setpoint": 700})
    codes = [w[1:4] for w in thermostat.writes()]
    assert codes == [b"B74", b"B11", b"B74"]
    b11 = thermostat.writes()[1]
    record = b11[4:]
    assert len(record) == 48
    assert record[0] == 0x72  # 'r' user settings
    assert record[1] == 0x01 and record[40:42] == b"\x01\xff"  # no-PIN triple
    assert record[6:8] == b"\x07\x00"
    assert fc00.get("frost_setpoint") == 700


async def test_offset_and_admin_writes(thermostat):
    fc00 = thermostat.device.endpoints[9].in_clusters[0xFC00]
    await fc00.write_attributes({"temperature_offset": -50})
    assert thermostat.writes()[1][4 + 3 : 4 + 5] == b"\x80\x50"
    assert fc00.get("temperature_offset") == -50
    await fc00.write_attributes({"heat_control": salus.HeatControl.itlc_radiator})
    b11 = thermostat.writes()[-2]
    assert b11[1:4] == b"B11" and b11[4] == 0x73 and b11[4 + 12] == 1
    await fc00.write_attributes({"display_resolution": salus.DisplayResolution.tenth_degree})
    assert fc00.get("display_resolution") == salus.DisplayResolution.tenth_degree
    thermo = thermostat.device.endpoints[9].in_clusters[Thermostat.cluster_id]
    await thermo.write_attributes({"max_heat_setpoint_limit": 2500})
    b11 = thermostat.writes()[-2]
    assert b11[4] == 0x73 and b11[4 + 22 : 4 + 24] == b"\x25\x00"
    assert thermo.get("max_heat_setpoint_limit") == 2500
    with pytest.raises(salus.SalusError):
        await thermo.write_attributes({"min_heat_setpoint_limit": 2500})


async def test_settings_not_written_with_pin(thermostat):
    settings = bytearray(FACTORY_SETTINGS)
    settings[40:42] = b"\x0e\x25"  # some PIN
    thermostat.settings = settings
    fc00 = thermostat.device.endpoints[9].in_clusters[0xFC00]
    with pytest.raises(salus.SalusError, match="PIN"):
        await fc00.write_attributes({"frost_setpoint": 700})
    assert [w[1:4] for w in thermostat.writes()] == [b"B74"]


async def test_no_echo_times_out(thermostat):
    thermostat.echo = False
    fc00 = thermostat.device.endpoints[9].in_clusters[0xFC00]
    with mock.patch.object(salus, "ECHO_TIMEOUT", 0.05), pytest.raises(salus.SalusError):
        await fc00.write_attributes({"child_lock": 1})


async def test_gateway_basic_answers_model(thermostat):
    hdr = foundation.ZCLHeader.general(
        tsn=0x11,
        command_id=foundation.GeneralCommand.Read_Attributes,
        direction=foundation.Direction.Client_to_Server,
    )
    thermostat.device.packet_received(
        t.ZigbeePacket(
            profile_id=260,
            cluster_id=0x0000,
            src_ep=9,
            dst_ep=1,
            data=t.SerializableBytes(hdr.serialize() + b"\x05\x00"),
        )
    )
    await asyncio.sleep(0.05)
    src_ep, dst_ep, cmd, hdr_bytes, payload = thermostat.sent[-1]
    assert (src_ep, dst_ep, cmd) == (1, 9, 0x01)
    assert hdr_bytes[0] == 0x18  # general, server to client, no default response
    assert payload == b"\x05\x00\x00\x42\x07SAU2AG1"


@pytest.fixture
def wiring_centre(app):
    device = make_device(
        app, WC_IEEE, 0x5678, "Computime", None, 8, [0x0000, 0xFC00], [0x0019]
    )
    return SimulatedSalusDevice(device, 8, direction=foundation.Direction.Client_to_Server)


def zone_table(zones: dict[int, int]) -> bytes:
    table = bytearray(b"\x8aB70" + bytes(5) + b"\x08")
    for zone in range(1, 13):
        nwk = zones.get(zone, 0xFFFF)
        table += nwk.to_bytes(2, "big") + b"\x00"
    table += b"\xff\xdb\x00\x7e\xaa\xaa\xa8\x80\x00\x00\x00\x00\x78"
    return bytes(table)


async def test_wiring_centre(wiring_centre):
    ep = wiring_centre.device.endpoints[8]
    assert isinstance(ep.in_clusters[0xFC00], salus.SalusWiringCentreCluster)
    wiring_centre.receive(salus.CMD_SYNC_REQUEST, bytes.fromhex("ff01ff00000000"))
    await asyncio.sleep(0.05)
    sync = [s for s in wiring_centre.sent if s[2] == salus.CMD_SYNC_RESPONSE][-1]
    assert sync[4][0] == 0x01 and sync[:2] == (1, 8)
    assert any(w[1:4] == b"B70" for w in wiring_centre.writes())
    wiring_centre.receive(salus.CMD_ECHO, zone_table({8: 0x1234}))
    fc00 = ep.in_clusters[0xFC00]
    assert fc00.get("zone_8") == "0x1234"
    assert fc00.get("zone_1") == "free"


async def test_zone_pairing(thermostat, wiring_centre):
    thermostat.zone_table = zone_table({1: 0x1234, 4: 0x1234})
    # the wiring centre's zone table arrives at the wiring centre, not the thermostat
    def table_from_wc(command_id, payload, cluster=salus.SALUS_CLUSTER_ID):
        if payload[1:4] == b"B70":
            wiring_centre.receive(command_id, payload)
        else:
            SimulatedSalusDevice.receive(thermostat, command_id, payload, cluster)

    thermostat.receive = table_from_wc
    fc00 = thermostat.device.endpoints[9].in_clusters[0xFC00]
    await fc00.pair_wiring_centre_zones("1,4", replace=True)
    b47 = [w for w in thermostat.writes() if w[1:4] == b"B47"]
    assert [w[4 + 6] for w in b47] == [0xFD, 0xFE]
    args = b47[-1][4:]
    assert len(args) == 33
    assert args[:8] == b"\x00\x11\x01\x00\x78\x56\xfe\x01"
    assert args[8:16] == b"\x01\x00\x00\x01\x00\x00\x00\x00"
    wc_fc00 = wiring_centre.device.endpoints[8].in_clusters[0xFC00]
    assert wc_fc00.get("zone_1") == str(THERMOSTAT_IEEE)


async def test_wiring_centre_request(thermostat, wiring_centre):
    thermostat.receive(salus.CMD_WC_REQUEST, b"\x01")
    await asyncio.sleep(0.05)
    answer = [s for s in thermostat.sent if s[2] == salus.CMD_WC_RESPONSE][-1]
    assert answer[4] == b"\x01\x78\x56"

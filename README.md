# Salus iT600 thermostats for Home Assistant ZHA

Use Salus iT600 "Quantum" thermostats (SQ610RF, SQ610RFNH) and the KL08RF wiring centre
in Home Assistant's built-in Zigbee integration (ZHA), without the Salus gateway.

This is a ZHA custom quirk: a single Python file that teaches ZHA how these devices
talk. It is a port of the zigbee2mqtt converter by **Mikkel Eskildsen**,
[mikkelke/salus-zigbee](https://github.com/mikkelke/salus-zigbee), and all the reverse
engineering behind it is his work (see [Credits](#credits)).

With the quirk, a thermostat shows up in Home Assistant as a normal climate device with
temperature, setpoint, on/off and humidity, plus its settings. The mains-powered models
(SQ610RF and the KL08RF) are Zigbee routers. Once they have joined, they also extend your
Zigbee network for your other devices.

> **Status: experimental, shared as is.** This is a hobby project with no support or
> guarantee. It has been used in real life with an **SQ610RF Quantum (230 V)** thermostat
> on ZHA with a **Sonoff Zigbee gateway**, and works fine there. It is also tested
> against simulated devices on several versions of the ZHA libraries, and the bytes it
> sends match captures from a real Salus system. Other setups may behave differently: try
> it on one device first, and if you like, [share how it went](#reporting-a-problem),
> whether it worked or not.
>
> **Read the [Disclaimer](#disclaimer) before using this project.** It is provided as is,
> without any warranty, and you use it entirely at your own risk.

## No Salus gateway needed

When the quirk is used in ZHA with a standard Zigbee coordinator (a "universal" Zigbee
gateway or USB stick, such as the Sonoff one it was tested with), **the Salus gateway
is not needed at all**. The thermostats join your own Zigbee network and talk directly
to that coordinator. The quirk answers them the way the Salus gateway would. No Salus
gateway, Salus cloud account or Salus app is involved. If you use a KL08RF wiring centre,
it joins the same network, and the thermostats talk to it directly too.

## Contents

- [No Salus gateway needed](#no-salus-gateway-needed)
- [Devices](#devices)
- [Requirements](#requirements)
- [Before you start](#before-you-start)
- [Install](#install)
- [Pair a thermostat](#pair-a-thermostat)
- [Add a wiring centre](#add-a-wiring-centre)
- [What you get in Home Assistant](#what-you-get-in-home-assistant)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Reporting a problem](#reporting-a-problem)
- [Advanced: actions](#advanced-actions)
- [How it works](#how-it-works)
- [Development and tests](#development-and-tests)
- [Credits](#credits) · [License](#license) · [Disclaimer](#disclaimer)

## Devices

The quirk is written for these devices. The last column is what is known so far, not a
promise that it works with yours.

| Device | Power | Zigbee role | What is known so far |
| --- | --- | --- | --- |
| SQ610RF (Quantum, 230 V) | Mains | Router | Worked in real use on ZHA with a Sonoff Zigbee gateway |
| SQ610RFNH (Quantum, battery) | Battery | End device | The protocol worked on zigbee2mqtt in the original project |
| SQ610, SQ610NH | | | Matched by model name; not tried |
| KL08RF / it600WC wiring centre | Mains | Router | The protocol worked on zigbee2mqtt in the original project |

The quirk does nothing for other Salus devices (plugs, TRVs, receivers).

## Requirements

- Home Assistant with the **ZHA** integration and a working Zigbee coordinator.
- A recent Home Assistant. The quirk is tested with the ZHA libraries of mid 2025,
  spring 2026 and November 2026 (see [Development and tests](#development-and-tests)).
  Releases older than early 2024 cannot load it, because they lack the quirk API it is
  built on.
- Access to your Home Assistant configuration folder, the one that contains
  `configuration.yaml`.

zigbee2mqtt users don't need this quirk. Use the
[original converter](https://github.com/mikkelke/salus-zigbee) instead.

## Before you start

- **Pairing a thermostat to ZHA takes it off its current network.** That network is
  either the Salus gateway or, without one, the wiring centre's own network. If your
  thermostats switch the heating through a KL08RF wiring centre, the wiring centre has to
  join ZHA too, and every thermostat has to be paired to its zones again
  ([Add a wiring centre](#add-a-wiring-centre)). Until then, the zones will not follow
  the thermostats and the heating may not respond.
- **Start with one thermostat** and check that it works before moving the rest.
- **Note your current settings** (setpoints, schedules, heat control algorithm) before a
  factory reset. The reset returns the thermostat to factory settings.

## Install

### 1. Find your configuration folder

| Installation type | Configuration folder |
| --- | --- |
| Home Assistant OS, Supervised | `/config` (in the File editor, Samba or SSH add-on) |
| Home Assistant Container (Docker) | `/config` inside the container, which is the host folder you mapped to `/config` |
| Home Assistant Core (Python venv) | The folder that holds `configuration.yaml`, often `/home/homeassistant/.homeassistant` |

The examples below use `/config`. On a Core installation, use your own folder's full
path instead. Use an absolute path, not a relative one.

### 2. Copy the quirk

Create a folder named `custom_zha_quirks` in the configuration folder and copy
[`salus_it600.py`](salus_it600.py) into it:

```text
/config/
├── configuration.yaml
└── custom_zha_quirks/
    └── salus_it600.py
```

On a Core installation, make sure the user that runs Home Assistant can read the folder,
for example `sudo chown -R homeassistant:homeassistant <folder>/custom_zha_quirks`.

### 3. Tell ZHA where the folder is

Add this to `configuration.yaml`. If you already have a `zha:` section, add only the
`custom_quirks_path` line to it.

```yaml
zha:
  custom_quirks_path: /config/custom_zha_quirks/
```

### 4. Restart Home Assistant and check the log

After the restart, the log (**Settings → System → Logs**, or `home-assistant.log`)
should contain:

```text
Salus iT600 quirk loaded (zigpy …, zha-quirks …, zha …)
```

If you see an error that mentions `salus_it600` instead, see
[Troubleshooting](#troubleshooting).

## Pair a thermostat

1. Factory reset the thermostat (see its installer manual).
2. In Home Assistant, go to **Settings → Devices & services → Zigbee Home Automation →
   Add device**. Use the general **Add device** button, not "Add device via this device".
   Keep the thermostat close to the coordinator or to a mains-powered Zigbee router.
3. Start pairing on the thermostat.
4. Open the new device. In the Zigbee info card, the **Quirk** line should mention
   `salus_it600`. Within a minute or two, the climate entity should show the room
   temperature.

Until a thermostat is paired with a receiver (such as a wiring centre zone), its display
shows "go to the Smart Home app" and its buttons do nothing. A Salus gateway does the
same. Control from Home Assistant works in that state.

If a thermostat was already in ZHA before you installed the quirk, you don't need to
pair it again. The quirk applies at the next restart.

## Add a wiring centre

The KL08RF / it600WC wiring centre switches the heating zones. The quirk handles one
wiring centre per Zigbee network.

1. **Factory reset the wiring centre:** hold **Pair** for 15 s until G1 and G2 turn red
   and go off. This also makes its paired thermostats leave their network.
2. **Cut its mains power** and wait until the network LED is dark. A short cut is not
   enough.
3. **Start Add device in ZHA** and switch the wiring centre on. It joins at once.
4. **Pair the thermostats** as described above.
5. **Pair each thermostat to its zones.** In **Developer tools → Actions**, switch to YAML
   mode and run:

   ```yaml
   action: zha.issue_zigbee_cluster_command
   data:
     ieee: "00:1e:5e:xx:xx:xx:xx:xx"   # the thermostat's IEEE address (on its device page)
     endpoint_id: 9
     cluster_id: 64512                 # 0xFC00
     cluster_type: in
     command: 224                      # pair_wiring_centre_zones
     command_type: server
     params:
       zones: "1,4"                    # zone numbers 1-8, comma separated
       replace: false
   ```

   The action succeeds when the wiring centre's zone table shows the thermostat in all
   the zones. It fails after 20 seconds per zone otherwise.

   Use `replace: true` to take over a zone that another thermostat still holds. You need
   it when a thermostat has re-joined the network with a new address.

The wiring centre's device page shows **Zone 1 thermostat** … **Zone 8 thermostat**,
each with the IEEE address of the thermostat in that zone, or `free`.

## What you get in Home Assistant

On each thermostat:

| Entity | What it does |
| --- | --- |
| Climate | Room temperature, target temperature, off / heat, heating / idle |
| Humidity | Relative humidity from the thermostat's sensor |
| Battery | Battery level (battery models only) |
| Hold type | Schedule, permanent hold, away or standby |
| Child lock | Locks the thermostat's buttons |
| Schedule setpoint | The temperature the thermostat's own schedule asks for now |
| Frost setpoint | The temperature held while the thermostat is off |
| Temperature offset | Calibration of the room temperature, −3.5 to +3.5 °C |
| Heat control algorithm | ITLC for underfloor, radiators or electric heating, ±0.25 / ±0.5 °C span, or THB actuator |
| Display resolution | Temperature step on the display, 0.5 or 0.1 °C |
| Min / max heat setpoint limit | The range the thermostat accepts |

The thermostat's clock and summer time are set from Home Assistant's time zone, and are
kept in sync.

## Troubleshooting

### The device page has no "Quirk" line

The quirk is not applied to that device. Check, in this order:

1. **Is the quirk loaded?** Look for `Salus iT600 quirk loaded` in the log after a
   restart.
   - No line at all: check that `custom_quirks_path` is under `zha:` in
     `configuration.yaml`, that the folder exists and contains `salus_it600.py`, and that
     Home Assistant can read it.
   - An error instead: see the next section.
2. **Does the device match?** The quirk applies to a device whose model is `SQ610RF`,
   `SQ610RFNH`, `SQ610` or `SQ610NH`, and which has cluster `0xfc00` on endpoint 9. Open
   the device, then **⋮ → Manage Zigbee device → Signature**. If your device reports a
   different model name, manufacturer or endpoint,
   [open an issue](#reporting-a-problem) with that signature.

### The log shows an error for `salus_it600`

- **`no quirks v2 API found … Installed: zigpy …, zha-quirks …, zha …`**: your Home
  Assistant is too old, or the ZHA packages in its environment don't match each other.
  Update Home Assistant. On a Core installation, also reinstall into a clean virtual
  environment.
- **`No module named 'zigpy.quirks.v2'`** (from an older copy of the file): the same
  cause. Update Home Assistant, and copy the latest `salus_it600.py`.
- **Anything else**: [open an issue](#reporting-a-problem) with the full error, from
  `Traceback` to the last line.

### A setting fails with "No … echo within 30 s"

The thermostat didn't confirm the command. Check that it is still online (its device
page shows when it was last seen), and try again. Battery models only receive commands
when they wake up, so they can take longer.

### Detailed logging

To see every message to and from the devices, add this to `configuration.yaml` and
restart:

```yaml
logger:
  logs:
    zigpy.zcl: debug
```

Each Salus message is logged as `Salus frame 0x..: <hex>`. Turn this off again
afterwards: it is very verbose.

## Limitations

- **Heat only.** The climate entity offers off and heat. The thermostat also has a cool
  mode, which the quirk displays correctly when the thermostat is in it, but cool can't
  be selected from the climate card.
- **PIN.** If a PIN is set on the thermostat, its settings (frost setpoint, offset,
  algorithm and so on) are not written, because a wrong PIN would lock the buttons.
  Setpoint, mode, hold type and child lock still work.
- **One wiring centre** per Zigbee network.
- **Coordinator identity.** The Salus gateway also reports Salus's manufacturer code in
  the coordinator's Zigbee node descriptor. A quirk can't change that, and the original
  project works without it.
- **Untested commands**, as in the original project: writing the minimum heating
  setpoint, setting the schedule setpoints, and how the thermostat reports changes made
  with its own buttons.

## Reporting a problem

There is no support for this project, but you are welcome to open an issue in this
repository. It may help others with the same device, even if no one replies. Include:

- your thermostat model and whether it is mains or battery powered
- your Home Assistant version (**Settings → About**)
- the `Salus iT600 quirk loaded (…)` line from the log, or the full error
- the device signature (**⋮ → Manage Zigbee device → Signature**)
- for wrong values or failed commands: a debug log (see
  [Detailed logging](#detailed-logging)) covering the moment it happened

Before posting, remove anything private from logs and signatures. IEEE addresses
identify your devices, and you may prefer to mask them.

A report that a device **works** is just as useful: it shows others what has worked so
far.

New findings about the protocol itself are welcome in the
[original project](https://github.com/mikkelke/salus-zigbee) too, so that both
projects benefit.

## Advanced: actions

Besides zone pairing, the quirk offers a few actions through
`zha.issue_zigbee_cluster_command`, with the same `endpoint_id`, `cluster_id`,
`cluster_type` and `command_type` as in the zone pairing example:

| Device | `command` | Name | `params` |
| --- | --- | --- | --- |
| Thermostat (endpoint 9) | 224 | `pair_wiring_centre_zones` | `zones`, `replace` |
| Thermostat (endpoint 9) | 225 | `send_b_command` | `hex_payload`, e.g. `"42323101"` |
| Thermostat (endpoint 9) | 226 | `refresh_settings` | none |
| Wiring centre (endpoint 8) | 224 | `request_zone_table` | none |
| Wiring centre (endpoint 8) | 225 | `send_b_command` | `hex_payload` |

`send_b_command` sends a raw Salus command, given as hex without the leading handle byte.
It is meant for protocol work: the original project's
[PROTOCOL.md](https://github.com/mikkelke/salus-zigbee/blob/main/PROTOCOL.md) lists the
commands. Sending the wrong bytes can change settings on the device.

## How it works

The devices (made by Computime, Zigbee manufacturer code 0x1078) use a private Zigbee
cluster, 0xFC00, and expect the coordinator to behave like a Salus gateway:

1. **Greeting.** After joining, a device sends a sync request. The quirk answers as the
   gateway does, with a device number and the local time. It also answers the
   thermostat's question for the coordinator's model name (`SAU2AG1`).
2. **Status.** The thermostat sends its state as records: live status (temperature,
   setpoint, mode, humidity, battery) and settings. The quirk keeps that state in
   standard Zigbee clusters inside Home Assistant (Thermostat, Relative humidity, Power
   configuration), so ZHA creates its usual entities for them.
3. **Commands.** Changes from Home Assistant are sent as Salus "B-commands", which the
   device confirms by echoing them back. Settings are changed by reading the settings
   record, changing one field and writing the record back.
4. **Wiring centre.** The quirk gives the wiring centre its device number, reads its zone
   table, and starts the pairing between a thermostat and its zones. After pairing,
   thermostat and wiring centre talk to each other directly.

The full protocol, with the evidence for every field, is in the original project's
[PROTOCOL.md](https://github.com/mikkelke/salus-zigbee/blob/main/PROTOCOL.md).

## Development and tests

[`tests/test_salus_it600.py`](tests/test_salus_it600.py) runs the quirk against a
simulated thermostat and a simulated wiring centre, which answer commands as the real
devices do. The tests cover:

- the greeting: the sync response for the protocol's example time matches the captured
  gateway frame byte for byte
- reading status and settings records
- the bytes of every command
- the settings write and its PIN check
- zone pairing

```sh
pip install zha pytest pytest-asyncio
pytest -o asyncio_mode=auto tests/
```

The tests pass on these library versions:

| zha | zha-quirks | zigpy | Home Assistant, roughly |
| --- | --- | --- | --- |
| 0.0.71 | 0.0.145 | 0.83 | mid 2025 |
| 1.4.1 | 1.2.0 | 1.5.1 | spring 2026 |
| 2.4.x | 2.4.x | 2.4.x | November 2026 |

The entities listed above were also checked with the ZHA library itself (2.4.x).

## Credits

This project would not exist without
[**salus-zigbee**](https://github.com/mikkelke/salus-zigbee) by
**Mikkel Eskildsen** ([@mikkelke](https://github.com/mikkelke)). He documented the
protocol in `PROTOCOL.md` and built the zigbee2mqtt converter that proved it on real
hardware. This quirk ports that converter to ZHA, and
everything it knows about the devices comes from his work:

- the greeting and sync response, the device numbers and the time zone table
- the status and settings record layouts, the number encodings and the PIN scrambling
- the command set, including the settings write and its no-PIN rules
- wiring centre zone pairing, the zone table and the replace sequence

His project is MIT licensed, and his copyright notice is kept in [LICENSE](LICENSE). If
this quirk is useful to you, please star or support the original project.

The quirk is built on [zigpy](https://github.com/zigpy/zigpy),
[zha](https://github.com/zigpy/zha) and
[zha-device-handlers](https://github.com/zigpy/zha-device-handlers), and uses their
public APIs.

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Maciej Kornacki and, for the original
converter and protocol work this is derived from, Copyright (c) 2026 Mikkel Eskildsen.

## Disclaimer

**Please read this before using the project. By using it, you accept these terms. If
you don't accept them, don't use it.**

### No warranty

This project is provided **"as is", without warranty of any kind**, express or implied,
including but not limited to the warranties of merchantability, fitness for a particular
purpose and non-infringement, as set out in the [LICENSE](LICENSE). There is no
guarantee that it works, that it works correctly, or that it keeps working with future
versions of Home Assistant, ZHA or the devices' firmware.

### No liability

To the fullest extent permitted by applicable law, **the authors and contributors are
not liable for any damage or loss** arising from the use of, or the inability to use,
this project or the information in it. That includes, without limitation:

- heating that fails, switches at the wrong time or runs uncontrolled
- frozen or burst pipes, water damage, overheating, or damage to floors, buildings or
  other property
- damage to the thermostats, wiring centre, boiler, pumps, actuators or other equipment
- higher energy costs
- loss of the manufacturer's warranty, support or service
- data loss, or problems with your Home Assistant installation or Zigbee network

### You are responsible for your installation

- You use this project **entirely at your own risk**, and you are responsible for
  deciding whether it is suitable for your installation.
- This project is **not a safety device**. Don't rely on it for frost protection, for
  the safety of people, animals or property, or in any safety-critical situation. Keep
  the protections of your heating system (such as frost, overheat and boiler safety
  features) in place and independent of Home Assistant.
- Keep a way to control your heating **without Home Assistant**, at least until you are
  confident it works for you, and check your heating regularly, especially in cold
  weather or when you are away.
- Any electrical work on heating equipment must be done by a qualified person, in line
  with local regulations. This project does not cover wiring or installation.
- Using devices outside the manufacturer's ecosystem may affect the manufacturer's
  warranty or support. Check this yourself before you start.

### No support or obligation

This is a personal hobby project, shared for free in the hope that it is useful. The
authors have **no obligation** to provide support, answer questions, fix problems or
publish updates.

### Not affiliated with Salus

This project is **independent**. It is not affiliated with, endorsed by, sponsored by or
supported by Salus Controls, Computime or any of their affiliates. "Salus", "iT600",
"Quantum" and the model names are trademarks or names of their respective owners. They
are used only to identify the devices the project is meant to work with. No Salus
software, firmware, images or other materials are included in this project.

### How the devices are accessed

The quirk **does not modify, replace or circumvent** the devices' firmware or any
security measure. The devices join a Zigbee network with the standard Zigbee procedure,
and the quirk communicates with them using their own messages, as the Salus gateway does,
for the purpose of interoperability. The protocol knowledge it uses comes from the
publicly available [salus-zigbee](https://github.com/mikkelke/salus-zigbee) project.

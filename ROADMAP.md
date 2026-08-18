# Roadmap

## v1 (current)

Serial (USB) and TCP (WiFi) transports, DMs and channels, mention gating,
byte-accurate LoRa chunking, Hermes-native access control, cron delivery,
and four mesh telemetry tools.

## Not in v1

### BLE transport

The Python `meshtastic` library supports BLE (`BLEInterface`), so this is
mostly plumbing plus a device-discovery step in the setup wizard. BLE pairing
is stateful and platform-specific (BlueZ vs CoreBluetooth), which is why it is
not in v1 rather than a lack of library support.

Configuring `transport: ble` today fails validation with an explicit
"not supported in v1" error — never silently.

### MQTT transport

Meshtastic can bridge over MQTT, which would let Hermes reach a mesh it has no
local radio for. It needs its own connection lifecycle, topic layout, and
credential handling, and — importantly — its own security discussion: MQTT
moves mesh traffic off the radio and onto someone else's broker.

Also rejected explicitly at validation time today.

### Proactive alerts

Push a message to the mesh when something happens (a node goes quiet, battery
drops below a threshold, a new node appears). The telemetry cache already holds
the history this needs; what is missing is a rule/threshold configuration
surface and a delivery policy that respects airtime.

### Other candidates

- **Position-aware tools** — "who is within 5 km", distance/bearing between
  nodes, subject to the same position-privacy rules.
- **Traceroute** — expose Meshtastic's traceroute for path debugging.
- **Store-and-forward** — surface router-node history for messages missed
  while the gateway was down.

# Local Application IPC

MeshHermes can expose one configured Meshtastic channel to a local application
over a same-user Unix socket. This lets applications such as Meshagatchi own
their domain logic without opening a second radio connection.

## Configuration

Set these values in the Hermes environment or platform configuration:

```text
MESHTASTIC_IPC_SOCKET=/run/user/1000/meshhermes.sock
MESHTASTIC_IPC_CHANNEL=in.secure
MESHTASTIC_IPC_MAX_MESSAGE_BYTES=200
```

The socket is optional. MeshHermes resolves the channel name against the radio
channel table when the radio connects. If the channel is missing or the message
limit is invalid, IPC stays disabled and the radio gateway continues normally.

## Protocol

The socket uses newline-delimited UTF-8 JSON. The current protocol version is
`1`.

The server sends one handshake after connection:

```json
{"type":"hello","version":1,"node_id":"!aabbccdd","channel_name":"in.secure","channel_index":1}
```

For each non-DM text message on the configured channel, the server sends:

```json
{"type":"message","version":1,"text":"/status","sender_id":"!11223344","message_id":"42","channel_name":"in.secure","channel_index":1,"is_self":false}
```

Applications send text with an explicit channel identity:

```json
{"op":"send","version":1,"text":"status reply","channel_name":"in.secure","channel_index":1}
```

The server replies with a send result:

```json
{"type":"send_result","version":1,"ok":true}
```

Malformed requests, protocol mismatches, channel mismatches, and oversized
messages are rejected. The gateway applies its normal transmit policy and
airtime limits to accepted sends.

## Ownership

MeshHermes owns the radio connection, channel resolution, socket lifecycle, and
send policy. The application owns command parsing, state, and application-level
behavior. Hermes does not interpret the application payload.

"""JSON schemas for the mesh_* agent tools.

Descriptions are written for the model, not for a human reading docs.  In
particular ``mesh_send`` states plainly that it drives a physical radio with
limited, regulated airtime — without that, a model treats it like any other
chat API and floods the band.
"""

MESH_NODES_SCHEMA = {
    "name": "mesh_nodes",
    "description": (
        "List the Meshtastic nodes this radio currently knows about. "
        "Returns each node's ID, names, hop count, signal quality (SNR/RSSI), "
        "battery level and when it was last heard. Read-only — transmits "
        "nothing. GPS positions are rounded and may be suppressed by the "
        "operator's privacy settings."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum nodes to return (default 50).",
            },
            "sort_by": {
                "type": "string",
                "enum": ["last_heard", "name", "hops", "snr"],
                "description": "Sort order (default last_heard, most recent first).",
            },
        },
        "required": [],
    },
}

MESH_TELEMETRY_SCHEMA = {
    "name": "mesh_telemetry",
    "description": (
        "Get device and environment telemetry for a Meshtastic node: battery "
        "level, voltage, channel utilization, air-utilization-TX, and "
        "temperature/humidity/pressure when the node has sensors. Includes "
        "recent history so you can answer trend questions. Read-only — "
        "transmits nothing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": (
                    "Node to query, as '!aabbccdd' hex, bare hex, or a decimal "
                    "node number. Omit to return telemetry for all known nodes."
                ),
            },
            "history": {
                "type": "integer",
                "description": "Number of recent samples to include per node (default 5, max 100).",
            },
        },
        "required": [],
    },
}

MESH_CHANNELS_SCHEMA = {
    "name": "mesh_channels",
    "description": (
        "List the channels configured on this Meshtastic radio, with each "
        "channel's index, name, and whether it is the primary channel. "
        "Read-only — transmits nothing."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

MESH_SEND_SCHEMA = {
    "name": "mesh_send",
    "description": (
        "Transmit a text message over the LoRa mesh radio to a specific node "
        "or channel.\n\n"
        "IMPORTANT: this drives a PHYSICAL RADIO. Airtime is a scarce, shared, "
        "legally regulated resource: each message takes seconds to transmit, "
        "is limited to ~200 bytes (longer text is split into multiple slow "
        "transmissions), and is heard by every node in range. Use this "
        "deliberately for messages the user explicitly wants sent over the "
        "mesh — never for chatter, acknowledgements, or status updates. "
        "Sending is rate-limited and restricted by the operator's policy; a "
        "refusal means the target is not permitted, not that you should retry."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Destination: a node ID ('!aabbccdd') for a direct message, "
                    "or 'channel:<name>' (e.g. 'channel:LongFast') to broadcast "
                    "on a channel."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "Plain text to transmit. Keep it short — every byte costs "
                    "airtime. Markdown is stripped before sending."
                ),
            },
        },
        "required": ["target", "message"],
    },
}

ALL_SCHEMAS = [
    MESH_NODES_SCHEMA,
    MESH_TELEMETRY_SCHEMA,
    MESH_CHANNELS_SCHEMA,
    MESH_SEND_SCHEMA,
]

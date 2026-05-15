"""
mqtt_publisher.py
=================

Sample publisher for the AIO MQTT broker (`aio-lb-broker`) running in the k3s
cluster's `azure-iot-operations` namespace. Modeled on the `lerobot-leader`
module from BillmanH/learn-iot:
  https://github.com/BillmanH/learn-iot/blob/experiment/layered_network/modules/lerobot-leader/lerobot-leader.md

Differences vs that module:
    - Port is 11000 (this cluster's `aio-lb-broker`), not 1883.
    - No robot hardware required: this script publishes a fixed set of
      dummy joint values so it can be run as a stand-alone sample.
    - Pure Linux / k3s host, no AKS Edge Essentials.

Quick start
-----------
    # 1) Install the client lib (one-time, user-local is fine):
    python3 -m pip install --user paho-mqtt==2.1.0

    # 2) Verify the broker service is reachable from this host:
    kubectl -n azure-iot-operations get svc aio-lb-broker
    # -> EXTERNAL-IP 192.168.1.197 PORT 11000:NNNNN/TCP

    # 3) Run with defaults (publishes to robot/leader/joint_positions @ 5 Hz):
    python3 mqtt_publisher.py

    # 4) In another terminal, subscribe to confirm:
    mosquitto_sub -h 192.168.1.197 -p 11000 -t 'robot/leader/#' -v

Common overrides via env vars (no flag parsing kept on purpose, to keep the
sample short):
    MQTT_BROKER     hostname or IP of the listener  (default 192.168.1.197)
    MQTT_PORT       TCP port                        (default 11000)
    MQTT_TOPIC      topic to publish on             (default robot/leader/joint_positions)
    MQTT_QOS        0 / 1 / 2                       (default 0)
    LEADER_ID       payload identifier              (default sample-leader)
    LOOP_HZ         publish rate                    (default 5.0)
"""
import json
import os
import signal
import socket
import sys
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt


# --- Configuration (env-driven, with sensible cluster-local defaults) -------
MQTT_BROKER = os.environ.get("MQTT_BROKER", "192.168.1.197")  # aio-lb-broker EXTERNAL-IP
MQTT_PORT   = int(os.environ.get("MQTT_PORT", "11000"))       # aio-lb-broker port
MQTT_TOPIC  = os.environ.get("MQTT_TOPIC", "robot/health")
MQTT_QOS    = int(os.environ.get("MQTT_QOS", "0"))            # 0=fire-and-forget
LEADER_ID   = os.environ.get("LEADER_ID", "sample-leader")
LOOP_HZ     = float(os.environ.get("LOOP_HZ", "5.0"))

# Joint names follow the SO101 layout used in the reference doc, so a
# downstream consumer that's built for lerobot-leader will see a familiar
# message shape. Values are fixed dummies (servo-tick units, 0..4095 range
# typically centered around 2048) - just enough to exercise the wire format.
DUMMY_JOINTS = {
    "shoulder_pan":  2048.0,
    "shoulder_lift": 2048.0,
    "elbow_flex":    2048.0,
    "wrist_flex":    2048.0,
    "wrist_roll":    2048.0,
    "gripper":       2048.0,
}


def dummyJointPositions() -> dict:
    """Return a fixed set of dummy joint values.

    No hardware, no time dependence - every published message carries the
    same joint dict. Replace with a real reader (e.g. `MyCobot280.get_angles()`
    or a `lerobot` leader API) when you want live data.
    """
    return dict(DUMMY_JOINTS)


def buildPayload(sequence: int) -> dict:
    """Build one MQTT message payload matching the lerobot-leader schema."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "leader_id": LEADER_ID,
        "sequence":  sequence,
        "joints":    dummyJointPositions(),
        "loop_hz":   LOOP_HZ,
    }


# --- MQTT client wiring -----------------------------------------------------
# `aio-lb-broker` is configured with no TLS and no authentication ref, so we
# connect over plain TCP with no credentials. If you ever swap to a listener
# that requires TLS or SAT, this is the spot to add `client.tls_set(...)` /
# `client.username_pw_set(...)`.
def onConnect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        print(f"connected to {MQTT_BROKER}:{MQTT_PORT}")
    else:
        # paho v2 passes a ReasonCode object; str() is human-readable.
        print(f"connect failed: {reason_code}", file=sys.stderr)


def onDisconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    print(f"disconnected ({reason_code})")


def main() -> int:
    # paho-mqtt 2.x: must opt in to a callback API version. v2 matches the
    # signatures above (extra `reason_code`/`properties` args).
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"{LEADER_ID}-{os.getpid()}",
        protocol=mqtt.MQTTv5,
    )
    client.on_connect = onConnect
    client.on_disconnect = onDisconnect

    # `connect()` blocks; `loop_start()` then takes over network I/O on a
    # background thread so we can sleep between publishes without starving
    # PINGREQ/keepalive traffic.
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=30)
    except (socket.gaierror, ConnectionRefusedError, OSError) as e:
        print(
            f"could not reach broker at {MQTT_BROKER}:{MQTT_PORT}: {e}\n"
            f"  - is the cluster up? `kubectl -n azure-iot-operations get svc aio-lb-broker`\n"
            f"  - is this host on the same network as the LB EXTERNAL-IP?",
            file=sys.stderr,
        )
        return 2
    client.loop_start()

    # Graceful Ctrl+C: flip a flag, let the loop exit, then disconnect.
    stopping = False

    def handleSigint(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, handleSigint)
    signal.signal(signal.SIGTERM, handleSigint)

    period = 1.0 / max(LOOP_HZ, 0.1)
    sequence = 0
    print(
        f"publishing to {MQTT_TOPIC} at {LOOP_HZ:.1f} Hz "
        f"(qos={MQTT_QOS}, leader_id={LEADER_ID}). Ctrl+C to stop."
    )
    try:
        while not stopping:
            payload = buildPayload(sequence)
            info = client.publish(
                MQTT_TOPIC,
                json.dumps(payload, separators=(",", ":")),
                qos=MQTT_QOS,
                retain=False,
            )
            # For QoS 0 the publish is fire-and-forget; `info.rc` still tells
            # us whether the message hit the local outgoing buffer.
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                print(f"publish failed: rc={info.rc}", file=sys.stderr)
            sequence += 1
            time.sleep(period)
    finally:
        client.loop_stop()
        client.disconnect()
        print(f"sent {sequence} message(s); bye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

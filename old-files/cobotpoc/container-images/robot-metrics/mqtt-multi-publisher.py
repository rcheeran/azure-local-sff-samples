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

    # 3) Run with defaults (publishes to robot/health, robot/coords,
    #    robot/robot_busy every 5 minutes):
    python3 mqtt_publisher.py

    # 4) In another terminal, subscribe to confirm:
    mosquitto_sub -h 192.168.1.197 -p 11000 -t 'robot/#' -v

Common overrides via env vars (no flag parsing kept on purpose, to keep the
sample short):
    MQTT_BROKER     hostname or IP of the listener  (default 192.168.1.197)
    MQTT_PORT       TCP port                        (default 11000)
    MQTT_HEALTH     health topic                    (default robot/health)
    MQTT_COORDS     coords topic                    (default robot/coords)
    MQTT_ROBOTBUSY  busy topic                      (default robot/robot_busy)
    MQTT_QOS        0 / 1 / 2                       (default 0)
    LEADER_ID        payload identifier              (default sample-asset)
    LOOP_PERIOD_S   seconds between publishes       (default 300.0, i.e. 5 min)
"""
import json
import os
import random
import signal
import socket
import sys
import threading
from datetime import datetime, timezone

import paho.mqtt.client as mqtt


# --- Configuration (env-driven, with sensible cluster-local defaults) -------
MQTT_BROKER     = os.environ.get("MQTT_BROKER", "192.168.1.197")  # aio-lb-broker EXTERNAL-IP
MQTT_PORT       = int(os.environ.get("MQTT_PORT", "11000"))       # aio-lb-broker port
MQTT_HEALTH     = os.environ.get("MQTT_HEALTH", "robot/health")
MQTT_COORDS     = os.environ.get("MQTT_COORDS", "robot/coords")
MQTT_ROBOTBUSY  = os.environ.get("MQTT_ROBOTBUSY", "robot/robot_busy")
MQTT_QOS        = int(os.environ.get("MQTT_QOS", "0"))            # 0=fire-and-forget
LEADER_ID       = os.environ.get("LEADER_ID", "mycobot280")
LOOP_PERIOD_S   = float(os.environ.get("LOOP_PERIOD_S", "150.0"))
LOOP_HZ     = float(os.environ.get("LOOP_HZ", "5.0"))


# Random target coordinates in the robot's base frame. Units: mm for
# inputX/inputY/inputZ, degrees for inputRz, plus a `safe` flag indicating
# whether the pose passed the safe-zone gate (the same envelope used by
# `verifyCoordsAreSafe` in cobotpoc.py: x >= -100 and 100 <= sqrt(x^2+y^2)
# <= 280). About a third of the time we deliberately produce something
# outside the safe envelope so downstream subscribers see both states.
# Replace with values fetched from your detector + transform pipeline when
# wiring up real data.
COORD_X_RANGE  = (-150.0, 250.0)   # mm
COORD_Y_RANGE  = (-200.0, 200.0)   # mm
COORD_Z_RANGE  = (0.0, 150.0)      # mm
COORD_RZ_RANGE = (-180.0, 180.0)   # degrees


def _isSafe(x: float, y: float) -> bool:
    """Mirror the safe-zone check used by cobotpoc.getTargetCoordsFromVision."""
    if x < -100.0:
        return False
    distance = (x ** 2 + y ** 2) ** 0.5
    return 100.0 <= distance <= 280.0


def dummyTargetCoords() -> dict:
    """Return a freshly-randomised target-pose dict each call.

    Values are drawn uniformly from the COORD_*_RANGE bounds; `safe` is
    derived from the same envelope cobotpoc.py uses, so subscribers will
    occasionally see safe=False without any extra wiring. Replace with a
    real source (e.g. the result of `getTargetCoordsFromVision()` from
    cobotpoc.py) when you want live data.
    """
    inputX  = round(random.uniform(*COORD_X_RANGE),  2)
    inputY  = round(random.uniform(*COORD_Y_RANGE),  2)
    inputZ  = round(random.uniform(*COORD_Z_RANGE),  2)
    inputRz = round(random.uniform(*COORD_RZ_RANGE), 2)
    return {
        "inputX":  inputX,
        "inputY":  inputY,
        "inputZ":  inputZ,
        "inputRz": inputRz,
        "safe":    _isSafe(inputX, inputY),
    }


def buildCoordsPayload(sequence: int) -> dict:
    """Build one MQTT message payload carrying a target pose (robot/coords)."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "leader_id":  LEADER_ID,
        "sequence":  sequence,
        "coords":    dummyTargetCoords(),
        "period_s":  LOOP_PERIOD_S,
    }

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


def buildHealthPayload(sequence: int) -> dict:
    """Build one MQTT message payload matching the lerobot-leader schema."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "leader_id": LEADER_ID,
        "sequence":  sequence,
        "joints":    dummyJointPositions(),
        "loop_hz":   LOOP_HZ,
    }


def buildBusyPayload(sequence: int) -> dict:
    """Build one MQTT message payload for the robot/robot_busy topic.

    Schema is `{isMoving: bool}` plus correlation metadata. Replace
    `random.choice` with `ROBOT.is_moving() == 1` (see
    pymycobot.mycobot280.MyCobot280) to wire up real robot state.
    """
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "leader_id":  LEADER_ID,
        "sequence":  sequence,
        "isMoving":  random.choice([True, False]),
        "period_s":  LOOP_PERIOD_S,
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

    # Graceful Ctrl+C / SIGTERM: a threading.Event is set by the signal
    # handler; the main loop uses Event.wait() instead of time.sleep() so
    # the wait is interrupted immediately. (PEP 475: time.sleep() retries
    # after a signal whose handler doesn't raise, so a long sleep would
    # otherwise block Ctrl+C until the period elapses.)
    stoppingEvent = threading.Event()

    def handleSignal(signum, frame):
        stoppingEvent.set()
    signal.signal(signal.SIGINT, handleSignal)
    signal.signal(signal.SIGTERM, handleSignal)

    period = max(LOOP_PERIOD_S, 0.1)
    sequence = 0
    print(
        f"publishing to {MQTT_HEALTH}, {MQTT_COORDS}, {MQTT_ROBOTBUSY} every {period:.1f}s "
        f"(qos={MQTT_QOS}, leader_id={LEADER_ID}). Ctrl+C to stop."
    )
    try:
        while not stoppingEvent.is_set():
            # Each tick publishes one message per topic. They share the
            # same sequence number / timestamp so subscribers can correlate.
            for topic, payload in (
                (MQTT_HEALTH,    buildHealthPayload(sequence)),
                (MQTT_COORDS,    buildCoordsPayload(sequence)),
                (MQTT_ROBOTBUSY, buildBusyPayload(sequence)),
            ):
                messageBody = json.dumps(payload, separators=(",", ":"))
                info = client.publish(
                    topic,
                    messageBody,
                    qos=MQTT_QOS,
                    retain=False,
                )
                # For QoS 0 the publish is fire-and-forget; `info.rc` still
                # tells us whether the message hit the local outgoing buffer.
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    print(f"publish to {topic} failed: rc={info.rc}", file=sys.stderr)
                else:
                    # Echo the published message so an operator running this
                    # in a terminal can see exactly what hit the broker.
                    print(f"[{topic}] {messageBody}", flush=True)
            sequence += 1
            # Sleep up to `period`, but wake immediately if Ctrl+C / SIGTERM
            # arrives. wait() returns True once the event is set.
            if stoppingEvent.wait(period):
                break
    except KeyboardInterrupt:
        # Belt-and-braces: if SIGINT somehow bypasses our handler
        # (e.g. signal arriving before signal.signal() returned).
        pass
    finally:
        client.loop_stop()
        client.disconnect()
        print(f"sent {sequence} message(s); bye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

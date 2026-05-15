# =========================================================================== #
# robot-metrics                                                                #
#                                                                              #
# Container wrapping `mqtt-multi-publisher.py`: a sample MQTT publisher that   #
# emits dummy robot health, coords, and busy-state telemetry to the AIO       #
# `aio-lb-broker` (azure-iot-operations namespace) on three topics:            #
#   robot/health, robot/coords, robot/robot_busy                               #
#                                                                              #
# All runtime config is env-driven (see the module docstring): MQTT_BROKER,    #
# MQTT_PORT, MQTT_HEALTH, MQTT_COORDS, MQTT_ROBOTBUSY, MQTT_QOS, LEADER_ID,    #
# LOOP_PERIOD_S, LOOP_HZ.                                                      #
#                                                                              #
# Build (run on this host where docker is installed):                          #
#   cd /home/clouduser/demo/cobotpoc/container-images/robot-metrics            #
#   sudo docker build --network=host -t robot-metrics:local .                  #
#                                                                              #
# Import into k3s containerd (skips needing a registry):                       #
#   sudo docker save robot-metrics:local \                                     #
#       | sudo /usr/local/bin/k3s ctr -n k8s.io images import -                #
#                                                                              #
# Verify it landed in the cluster's image store:                               #
#   sudo /usr/local/bin/k3s ctr -n k8s.io images ls | grep robot-metrics       #
#                                                                              #
# Run locally (without k8s) to smoke-test against the cluster's broker:        #
#   docker run --rm --network=host \                                           #
#       -e MQTT_BROKER=192.168.1.197 -e MQTT_PORT=11000 \                      #
#       robot-metrics:local                                                    #
# =========================================================================== #
FROM python:3.11-slim

# No system packages required: paho-mqtt is pure Python and the broker
# connection is plain TCP (no TLS, no auth, see comments in the script).

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY mqtt-multi-publisher.py /app/mqtt-multi-publisher.py

# Sensible cluster-local defaults; all overridable via Deployment env.
ENV MQTT_BROKER=192.168.1.197 \
    MQTT_PORT=11000 \
    MQTT_HEALTH=robot/health \
    MQTT_COORDS=robot/coords \
    MQTT_ROBOTBUSY=robot/robot_busy \
    MQTT_QOS=0 \
    LEADER_ID=robot-metrics \
    LOOP_PERIOD_S=60.0 \
    LOOP_HZ=5.0 \
    PYTHONUNBUFFERED=1

# Long-running publisher; no port to expose (outbound MQTT only).
CMD ["python", "/app/mqtt-multi-publisher.py"]

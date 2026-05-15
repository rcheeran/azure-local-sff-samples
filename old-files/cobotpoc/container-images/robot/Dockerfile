# =========================================================================== #
# test_robot.py container image                                                #
#                                                                              #
# Minimal Python image that talks to a MyCobot 280 over the host's USB         #
# serial port (/dev/ttyACM0). The Python code is in this same folder.          #
#                                                                              #
# Build (DNS needs host networking on this host \u2014 see                         #
# /memories/repo/build-host.md for why):                                       #
#   cd /home/clouduser/demo/cobotpoc/container-images/robot                    #
#   sudo docker build --network=host -t test-robot:local .                     #
#                                                                              #
# Run (the robot's serial port must be passed in):                             #
#   sudo docker run --rm -it \                                                 #
#       --device=/dev/ttyACM0 \                                                #
#       test-robot:local                                                       #
#                                                                              #
# Why --device instead of --privileged: the script only needs the one tty;     #
# --device gives the container exactly that, with no extra capabilities.       #
# If the serial port shows up under a different name (ttyACM1, ttyUSB0, ...)   #
# pass it explicitly:                                                          #
#       --device=/dev/ttyUSB0 -e ROBOT_PORT=/dev/ttyUSB0                       #
#                                                                              #
# Optional: import into k3s containerd if you want to deploy as a Job/Pod:     #
#   sudo docker save test-robot:local \                                        #
#       | sudo /usr/local/bin/k3s ctr -n k8s.io images import -                #
# =========================================================================== #
FROM python:3.12-slim

# `pymycobot` 4.x is the version we already vetted on the host. `pyserial` is
# pulled in transitively but pinning here makes the build deterministic.
# `numpy` is needed at import time by `pymycobot.elephantrobot` even though
# it's not declared as a hard dependency in pymycobot's wheel.
RUN pip install --no-cache-dir \
        "pymycobot==4.0.4" \
        "pyserial==3.5" \
        "numpy"

WORKDIR /app
COPY test_robot.py /app/test_robot.py

# Unbuffered stdout so `docker logs` shows progress in real time.
ENV PYTHONUNBUFFERED=1

CMD ["python3", "test_robot.py"]

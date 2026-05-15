"""
Compute the homography (transformation) needed between camera field of view (in px) and robot base coordinates (in mm).

Prerequisites:

- MyCobot 280 plugged in, with pymycobot and its dependencies installed
- USB camera plugged in, mounted overhead, and OpenCV and its dependencies installed

Usage:

1) Specify robotCoords = [] for the robot arm to visit, in mm. The provided default should work.
2) As the robot visits each coordinate, mark the spot somehow, e.g., by placing an object there.
3) When the camera preview pops open, click each marked spot in the same order. Coordinates in px will be printed.
4) Copy both sets of coordinates into the transformCoords() function.
5) Optionally, use CAPTURE_WITH_ROBOT and CAPTURE_WITH_CAMERA to run half separately.
"""

import time
import cv2
from pymycobot.mycobot280 import MyCobot280

robotPort = "/dev/ttyACM0"
robotBaud = 115200
ROBOT = MyCobot280(robotPort, robotBaud)
# Wait briefly to ensure connection is established
time.sleep(1.5)

CAPTURE_WITH_ROBOT = True
CAPTURE_WITH_CAMERA = True

############################
### STEP 1 : ROBOT FRAME ###
############################

if CAPTURE_WITH_ROBOT:

    print("STEP 1: CAPTURE COORDINATES IN ROBOT FRAME")

    print("Reset robot to upright position...")
    ROBOT.set_gripper_value(35, 50) # Open gripper 35%
    ROBOT.send_angles([-30, 45, -45, 0, 30, -45], 60) # Yield pose
    time.sleep(3)

    robotCoords = [
        [-50, 200, 125, 180, 0, 45],
        [200, 200, 125, 180, 0, 45],
        [200, -200, 125, 180, 0, 45],
        [-50, -200, 125, 180, 0, 45],
    ]

    print("Robot Coordinates:")
    for coords in robotCoords:
        print(coords)

    for index, coords in enumerate(robotCoords, start=1):
        print(f"Moving to position {index}...")
        ROBOT.send_coords(coords, 30)
        time.sleep(10)
        ROBOT.send_angles([-30, 45, -45, 0, 30, -45], 60) # Return to yield pose
        time.sleep(3)

#############################
### STEP 2 : CAMERA FRAME ###
#############################

if CAPTURE_WITH_CAMERA:

    print("STEP 2: CAPTURE COORDINATES IN CAMERA FRAME")

    print("Camera Coordinates:")

    cameraIndex = 0
    displayScale = 2

    if displayScale <= 0:
        raise ValueError("displayScale must be greater than 0")

    cap = cv2.VideoCapture(cameraIndex)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open camera at index {cameraIndex}")

    windowName = "Calibration Click Capture (press q to quit)"
    state = {"frame": None}

    def onMouse(event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        frame = state["frame"]
        if frame is None:
            return

        frameH, frameW = frame.shape[:2]
        pixelX = int(round(x / displayScale))
        pixelY = int(round(y / displayScale))
        pixelX = max(0, min(frameW - 1, pixelX))
        pixelY = max(0, min(frameH - 1, pixelY))
        print(f"[{pixelX}, {pixelY}]")

    cv2.namedWindow(windowName)
    cv2.setMouseCallback(windowName, onMouse)

    try:
        while True:
            success, frame = cap.read()
            if not success:
                print("Failed to grab frame from camera")
                break

            state["frame"] = frame
            frameH, frameW = frame.shape[:2]
            display = cv2.resize(
                frame,
                (frameW * displayScale, frameH * displayScale),
                interpolation=cv2.INTER_NEAREST,
            )
            cv2.imshow(windowName, display)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        cap.release()
        cv2.destroyWindow(windowName)

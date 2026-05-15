"""
Opens a live camera preview.
Click any pixel to print its BGR and HSV values.
Use the printed HSV values to tune the ranges in vision functions.
Press 'q' to quit.
"""

import cv2

cap = cv2.VideoCapture(0)
if not cap.isOpened():
    raise RuntimeError("Could not open camera")

windowName = "Color Calibration - click a cube, press q to quit"
clickedHsv = [None]  # mutable container so the callback can write into it

def onMouse(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:
        frame = param["frame"]
        hsv = param["hsv"]
        bgrPixel = frame[y, x].tolist()
        hsvPixel = hsv[y, x].tolist()
        print(f"Pixel ({x}, {y})  BGR: {bgrPixel}  HSV: {hsvPixel}  "
                f"  -> H={hsvPixel[0]}, S={hsvPixel[1]}, V={hsvPixel[2]}")

cv2.namedWindow(windowName)
sharedData = {"frame": None, "hsv": None}
cv2.setMouseCallback(windowName, onMouse, sharedData)

print("calibrateColorRanges: click on each cube to read its HSV values. Press 'q' to quit.")
while True:
    success, frame = cap.read()
    if not success:
        print("Warning: failed to grab frame")
        continue

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    sharedData["frame"] = frame
    sharedData["hsv"] = hsv

    cv2.imshow(windowName, frame)
    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

cap.release()
cv2.destroyWindow(windowName)
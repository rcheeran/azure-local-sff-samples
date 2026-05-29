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
    import http.server
    import json as _json
    import socket
    import ssl
    import threading
    import urllib.request

    print("STEP 2: CAPTURE COORDINATES IN CAMERA FRAME")

    VISION_FRAME_URL = "https://vision.local/frame.jpg"
    UI_PORT = 8095
    NUM_POINTS = 4

    # Snapshot one frame from vision-service (it owns /dev/video0 in-cluster,
    # so we can't open the V4L2 device directly while the pod is running).
    sslCtx = ssl.create_default_context()
    sslCtx.check_hostname = False
    sslCtx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(VISION_FRAME_URL, context=sslCtx, timeout=10) as resp:
        jpegBytes = resp.read()
    print(f"Fetched frame: {len(jpegBytes)} bytes from {VISION_FRAME_URL}")

    clicks = []
    clicksLock = threading.Lock()
    doneEvent = threading.Event()

    HTML_PAGE = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Calibrate - click {NUM_POINTS} marked spots in order</title>
<style>
  body {{ background:#111; color:#eee; font-family:system-ui,sans-serif; margin:0; padding:12px; }}
  #status {{ font-size:18px; margin-bottom:8px; }}
  #wrap {{ position:relative; display:inline-block; }}
  img {{ display:block; max-width:100%; cursor:crosshair; user-select:none; }}
  .pin {{ position:absolute; transform:translate(-50%,-50%); color:#0f0;
          font-weight:bold; font-size:20px; pointer-events:none;
          text-shadow:0 0 3px #000,0 0 3px #000; }}
</style></head>
<body>
  <div id="status">Click point 1 of {NUM_POINTS} (the first marked spot)</div>
  <div id="wrap"><img id="frame" src="/frame.jpg" alt="camera frame"></div>
<script>
  const img = document.getElementById('frame');
  const wrap = document.getElementById('wrap');
  const status = document.getElementById('status');
  const NUM = {NUM_POINTS};
  let n = 0;
  img.addEventListener('click', async (ev) => {{
    if (n >= NUM) return;
    const rect = img.getBoundingClientRect();
    const dispX = ev.clientX - rect.left;
    const dispY = ev.clientY - rect.top;
    const sx = img.naturalWidth / rect.width;
    const sy = img.naturalHeight / rect.height;
    const px = Math.round(dispX * sx);
    const py = Math.round(dispY * sy);
    const pin = document.createElement('div');
    pin.className = 'pin';
    pin.style.left = dispX + 'px';
    pin.style.top = dispY + 'px';
    pin.textContent = (n + 1) + '+';
    wrap.appendChild(pin);
    n += 1;
    status.textContent = (n < NUM)
      ? `Click point ${{n+1}} of ${{NUM}} (the next marked spot)`
      : `All ${{NUM}} points captured. You can close this tab.`;
    await fetch('/click', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{x: px, y: py}})
    }});
  }});
</script>
</body></html>
"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = HTML_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/frame.jpg":
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpegBytes)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(jpegBytes)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            if self.path != "/click":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            payload = _json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            x = int(payload.get("x", 0))
            y = int(payload.get("y", 0))
            with clicksLock:
                clicks.append([x, y])
                idx = len(clicks)
                print(f"Point {idx}: [{x}, {y}]")
                if idx >= NUM_POINTS:
                    doneEvent.set()
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", UI_PORT), Handler)
    serverThread = threading.Thread(target=httpd.serve_forever, daemon=True)
    serverThread.start()

    # Best-effort: show this host's LAN IP so the user can paste it into a browser.
    hostIp = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        hostIp = s.getsockname()[0]
        s.close()
    except OSError:
        pass

    print()
    print(f"Open one of these URLs in a browser on a machine that can reach this host:")
    if hostIp:
        print(f"  http://{hostIp}:{UI_PORT}/")
    print(f"  http://{socket.gethostname()}:{UI_PORT}/")
    print(f"Click each of the {NUM_POINTS} marked spots in the same order the robot visited.")
    print(f"Press Ctrl+C to abort.")

    try:
        while not doneEvent.wait(timeout=1.0):
            pass
    except KeyboardInterrupt:
        print("Aborted by user.")
    finally:
        httpd.shutdown()
        httpd.server_close()

    print()
    print("Camera Coordinates (paste into transformCoordsFromCameraToRobot()):")
    print("cameraCorners = [")
    for [x, y] in clicks:
        print(f"    [{x}, {y}],")
    print("]")

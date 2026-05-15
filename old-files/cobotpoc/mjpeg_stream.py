"""MJPEG stream server for viewing camera feed over HTTP.

Usage:
    python3 mjpeg_stream.py

Then open http://localhost:8090 in a browser (VS Code will auto-forward the port).
"""

import glob
import os
import stat
import subprocess
import time
import cv2
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

HOST = "0.0.0.0"
PORT = 8090
BOUNDARY = b"--frame"

# Shared state for the latest JPEG frame
latest_frame = None
frame_lock = threading.Lock()
running = True


def ensure_device_access():
    """Ensure /dev/video* nodes are readable; chmod via sudo if not."""
    video_devices = sorted(glob.glob("/dev/video*"))
    if not video_devices:
        print("No video devices found under /dev/video*")
        return
    needs_fix = []
    for dev in video_devices:
        try:
            fd = os.open(dev, os.O_RDWR)
            os.close(fd)
        except (PermissionError, OSError):
            needs_fix.append(dev)
    if needs_fix:
        print(f"Fixing permissions on {needs_fix}...")
        subprocess.run(["sudo", "chmod", "666"] + needs_fix, check=False)


def open_capture_camera():
    """Find a /dev/video* node that actually returns frames.

    UVC cameras register multiple V4L2 nodes (capture + metadata); only one
    yields frames. This function probes each char device, tries the V4L2 backend
    first then falls back to the default (FFmpeg) backend, and accepts the
    first node from which cv2 can read a frame.
    """
    for dev in sorted(glob.glob("/dev/video*")):
        try:
            mode = os.stat(dev).st_mode
        except OSError as e:
            print(f"skip {dev}: stat failed ({e})")
            continue
        if not stat.S_ISCHR(mode):
            print(f"skip {dev}: not a character device")
            continue

        cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(dev)  # fall back to default backend
        if not cap.isOpened():
            print(f"skip {dev}: could not open")
            cap.release()
            continue

        ok, frame = cap.read()
        if ok and frame is not None:
            print(f"using camera {dev}")
            return dev, cap

        print(f"skip {dev}: opened but no frame returned (likely metadata node)")
        cap.release()

    return None, None


def capture_loop():
    """Continuously capture frames from the camera."""
    global latest_frame, running

    ensure_device_access()
    dev, cap = open_capture_camera()
    if cap is None:
        print("ERROR: No working capture camera found")
        running = False
        return

    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"Camera {dev} opened: {w}x{h}")

    while running:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.01)
            continue
        ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            with frame_lock:
                latest_frame = jpeg.tobytes()

    cap.release()


class MJPEGHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            html = (
                b"<html><body style='margin:0;background:#000'>"
                b"<img src='/stream' style='width:100%;height:auto'>"
                b"</body></html>"
            )
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            self.wfile.flush()
            return
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while running:
                    with frame_lock:
                        frame = latest_frame
                    if frame is None:
                        time.sleep(0.05)
                        continue
                    self.wfile.write(BOUNDARY + b"\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.033)  # ~30 fps cap
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass  # Suppress per-request logs


def main():
    global running

    cam_thread = threading.Thread(target=capture_loop, daemon=True)
    cam_thread.start()

    server = HTTPServer((HOST, PORT), MJPEGHandler)
    print(f"MJPEG stream listening on {HOST}:{PORT}")
    print(f"  Local:   http://localhost:{PORT}/")
    print(f"  Network: http://<this-host-ip>:{PORT}/")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
        running = False
        server.shutdown()


if __name__ == "__main__":
    main()

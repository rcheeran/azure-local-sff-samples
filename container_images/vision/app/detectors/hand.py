"""Hand detector -- port of lookForHand() from cobotpoc.py.

Uses MediaPipe HandLandmarker. The model is loaded once at server startup
and passed in by the caller (so we don't reload the .task file per request).
"""

from typing import Optional, Tuple

import cv2
import numpy as np

from app.hud import drawHud


# Index meanings come from the MediaPipe HandLandmarker spec.
# 0 = wrist, 5 = index_mcp, 9 = middle_mcp, 13 = ring_mcp, 17 = pinky_mcp.
# Averaging these five gives a stable approximation of the palm centroid.
_PALM_INDICES = (0, 5, 9, 13, 17)
_HANDOFF_RZ_DEG = 45


def detectHand(
    bgrFrame: np.ndarray,
    handLandmarker,
    handConnections,
) -> Tuple[Optional[Tuple[int, int, int, int]], np.ndarray, dict]:
    """Detect a single hand in a BGR frame.

    Args:
        bgrFrame:        BGR ndarray.
        handLandmarker:  mediapipe.tasks.python.vision.HandLandmarker
                         (created once at server startup).
        handConnections: list of mediapipe HandLandmarksConnections.HAND_CONNECTIONS,
                         passed in to keep this module import-light.
    """
    # Lazy import to keep the module loadable without mediapipe installed
    # (the unit tests can stub the landmarker).
    import mediapipe as mp  # type: ignore

    frameH, frameW = bgrFrame.shape[:2]
    rgbFrame = cv2.cvtColor(bgrFrame, cv2.COLOR_BGR2RGB)
    mpImage = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgbFrame)
    results = handLandmarker.detect(mpImage)

    annotated = bgrFrame.copy()
    detectedPose = None

    if results.hand_landmarks:
        handLandmarks = results.hand_landmarks[0]

        pixelX = int(sum(handLandmarks[i].x for i in _PALM_INDICES) / len(_PALM_INDICES) * frameW)
        pixelY = int(sum(handLandmarks[i].y for i in _PALM_INDICES) / len(_PALM_INDICES) * frameH)
        detectedPose = (pixelX, pixelY, 0, _HANDOFF_RZ_DEG)

        # Draw landmarks + skeleton.
        for lm in handLandmarks:
            lx, ly = int(lm.x * frameW), int(lm.y * frameH)
            cv2.circle(annotated, (lx, ly), 4, (0, 255, 0), -1)
        for connection in handConnections:
            startLm = handLandmarks[connection.start]
            endLm = handLandmarks[connection.end]
            cv2.line(
                annotated,
                (int(startLm.x * frameW), int(startLm.y * frameH)),
                (int(endLm.x * frameW), int(endLm.y * frameH)),
                (0, 255, 0),
                1,
            )
        cv2.circle(annotated, (pixelX, pixelY), 8, (0, 0, 255), -1)

    hudLines = ["detector=hand"]
    if detectedPose is not None:
        hudLines.append(f"cam(px): x={detectedPose[0]} y={detectedPose[1]} rz={detectedPose[3]}")
    else:
        hudLines.append("cam(px): no hand detected")
    drawHud(annotated, hudLines)

    return detectedPose, annotated, {"samples": 1}

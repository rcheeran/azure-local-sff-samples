"""Cube detector -- port of lookForCube() from cobotpoc.py.

Single-frame detection: takes a BGR ndarray, returns (pose, annotated_frame,
metadata). The multi-frame averaging loop from the original lives client-side
now (caller can send N requests if it wants stability).
"""

from typing import Optional, Tuple

import cv2
import numpy as np

from app.hud import drawHud


# Same HSV ranges as cobotpoc.py. These are calibrated against the actual
# cubes on the demo table -- do not "tidy them up".
_HSV_RANGES_BY_COLOR = {
    "red": [
        (np.array([164, 65, 65]),  np.array([180, 255, 255])),
        (np.array([0, 65, 65]),    np.array([10, 255, 255])),
    ],
    "green":  [(np.array([66, 100, 25]), np.array([94, 255, 255]))],
    "blue":   [(np.array([95, 130, 50]), np.array([123, 255, 255]))],
    "yellow": [(np.array([18, 50, 65]),  np.array([44, 255, 255]))],
}

_KNOWN_COLORS = ("red", "green", "blue", "yellow")


def _resolveColor(targetText: str) -> Optional[str]:
    """Pick a known color name out of free-form target text."""
    text = (targetText or "").lower()
    for color in _KNOWN_COLORS:
        if color in text:
            return color
    return None


def detectCube(
    bgrFrame: np.ndarray,
    targetText: str = "",
) -> Tuple[Optional[Tuple[int, int, int, int]], np.ndarray, dict]:
    """Detect a cube in a single BGR frame.

    Returns:
        pose: (x, y, z=0, rzDeg) in camera-pixel space, or None.
        annotated: BGR frame with contours / fitted rect / axis / HUD drawn.
        meta:     {"color": <resolved-color-or-None>, "samples": int}
    """
    blurKernel = (5, 5)
    previewBlurKernel = (3, 3)
    morphKernelSize = (7, 7)
    morphIterations = 2
    minContourArea = 400
    minAspectRatio = 0.70
    minRectFillRatio = 0.80
    axisLengthPx = 100

    targetColor = _resolveColor(targetText)
    if targetColor is not None:
        targetHsvRanges = _HSV_RANGES_BY_COLOR[targetColor]
    else:
        targetHsvRanges = []
        for ranges in _HSV_RANGES_BY_COLOR.values():
            targetHsvRanges.extend(ranges)

    blurred = cv2.GaussianBlur(bgrFrame, blurKernel, 0)
    hsvFrame = cv2.cvtColor(blurred, cv2.COLOR_BGR2HSV)

    colorMask = np.zeros(hsvFrame.shape[:2], dtype=np.uint8)
    for lowerBound, upperBound in targetHsvRanges:
        colorMask = cv2.bitwise_or(colorMask, cv2.inRange(hsvFrame, lowerBound, upperBound))

    morphKernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, morphKernelSize)
    colorMask = cv2.morphologyEx(colorMask, cv2.MORPH_CLOSE, morphKernel, iterations=morphIterations)

    maskContours, _ = cv2.findContours(colorMask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

    bestContour = None
    bestRect = None
    rectAngleDeg = None
    rectWidth = None
    rectHeight = None
    centerX = None
    centerY = None
    detectedPose = None

    if maskContours:
        bestScore = None
        for candidateContour in maskContours:
            contourArea = cv2.contourArea(candidateContour)
            if contourArea < minContourArea:
                continue

            contourRect = cv2.minAreaRect(candidateContour)
            (_, _), (candidateWidth, candidateHeight), _ = contourRect
            if candidateWidth <= 0 or candidateHeight <= 0:
                continue

            rectAspectRatio = min(candidateWidth, candidateHeight) / max(candidateWidth, candidateHeight)
            if rectAspectRatio < minAspectRatio:
                continue

            rectCorners = cv2.boxPoints(contourRect)
            rectCorners = np.intp(rectCorners)
            rectMask = np.zeros(colorMask.shape, dtype=np.uint8)
            cv2.fillPoly(rectMask, [rectCorners], 255)
            rectAreaPx = cv2.countNonZero(rectMask)
            if rectAreaPx == 0:
                continue
            coloredAreaPx = cv2.countNonZero(cv2.bitwise_and(colorMask, rectMask))
            rectFillRatio = coloredAreaPx / rectAreaPx
            if rectFillRatio < minRectFillRatio:
                continue

            score = contourArea * (rectAspectRatio ** 3) * (rectFillRatio ** 3)
            if bestScore is None or score > bestScore:
                bestScore = score
                bestContour = candidateContour

        if bestContour is not None:
            contourMoments = cv2.moments(bestContour)
            if contourMoments["m00"] != 0:
                centerX = int(contourMoments["m10"] / contourMoments["m00"])
                centerY = int(contourMoments["m01"] / contourMoments["m00"])
                bestRect = cv2.minAreaRect(bestContour)
                (_, _), (rectWidth, rectHeight), rectAngleDeg = bestRect
                graspRzDeg = int(rectAngleDeg % 90.0)
                detectedPose = (centerX, centerY, 0, graspRzDeg)

    # ---- Annotated preview frame ---------------------------------------------
    annotated = cv2.GaussianBlur(bgrFrame.copy(), previewBlurKernel, 0)
    cv2.drawContours(annotated, maskContours, -1, (0, 255, 255), 1)

    if bestRect is not None:
        rectCorners = cv2.boxPoints(bestRect)
        rectCorners = np.intp(rectCorners)
        cv2.drawContours(annotated, [rectCorners], 0, (255, 0, 255), 2)

    if centerX is not None and centerY is not None:
        cv2.circle(annotated, (centerX, centerY), 5, (0, 0, 255), -1)
        if rectAngleDeg is not None and rectWidth is not None and rectHeight is not None:
            majorAxisDeg = rectAngleDeg if rectWidth >= rectHeight else rectAngleDeg + 90
            halfAxisPx = axisLengthPx / 2
            majorAxisRad = np.deg2rad(majorAxisDeg)
            axisDx = int(np.cos(majorAxisRad) * halfAxisPx)
            axisDy = int(np.sin(majorAxisRad) * halfAxisPx)
            cv2.line(
                annotated,
                (centerX - axisDx, centerY - axisDy),
                (centerX + axisDx, centerY + axisDy),
                (255, 255, 0),
                2,
            )

    hudLines = [f"detector=cube  color={targetColor or 'any'}"]
    if detectedPose is not None:
        hudLines.append(f"cam(px): x={detectedPose[0]} y={detectedPose[1]} rz={detectedPose[3]}")
    else:
        hudLines.append("cam(px): no cube detected")
    drawHud(annotated, hudLines)

    return detectedPose, annotated, {"color": targetColor, "samples": 1}

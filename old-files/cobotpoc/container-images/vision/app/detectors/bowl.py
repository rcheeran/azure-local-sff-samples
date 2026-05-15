"""Bowl detector -- port of lookForBowl() from cobotpoc.py.

Single-frame detection. Round shape via circularity + circle fill ratio.
"""

from typing import Optional, Tuple

import cv2
import numpy as np

from app.hud import drawHud


# Identical HSV ranges to lookForBowl. White is included for bowls only.
_HSV_RANGES_BY_COLOR = {
    "red": [
        (np.array([164, 65, 65]),  np.array([180, 255, 255])),
        (np.array([0, 65, 65]),    np.array([10, 255, 255])),
    ],
    "green":  [(np.array([66, 100, 25]),  np.array([94, 255, 255]))],
    "blue":   [(np.array([95, 130, 50]),  np.array([123, 255, 255]))],
    "yellow": [(np.array([18, 50, 65]),   np.array([44, 255, 255]))],
    "white":  [(np.array([0, 0, 180]),    np.array([180, 50, 255]))],
}

_KNOWN_COLORS = ("red", "green", "blue", "yellow", "white")


def _resolveColor(targetText: str) -> Optional[str]:
    text = (targetText or "").lower()
    for color in _KNOWN_COLORS:
        if color in text:
            return color
    return None


def detectBowl(
    bgrFrame: np.ndarray,
    targetText: str = "",
) -> Tuple[Optional[Tuple[int, int, int, int]], np.ndarray, dict]:
    """Detect a bowl-like round object in a single BGR frame."""
    blurKernel = (5, 5)
    previewBlurKernel = (3, 3)
    morphKernelSize = (7, 7)
    morphIterations = 2
    minContourArea = 900
    minCircularity = 0.65
    minCircleFillRatio = 0.75
    handoffRzDeg = 45  # round objects have no inherent orientation

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
    bestCircle = None
    centerX = None
    centerY = None
    detectedPose = None

    if maskContours:
        bestScore = None
        for candidateContour in maskContours:
            contourArea = cv2.contourArea(candidateContour)
            if contourArea < minContourArea:
                continue

            contourPerimeter = cv2.arcLength(candidateContour, True)
            if contourPerimeter <= 0:
                continue
            circularity = (4.0 * np.pi * contourArea) / (contourPerimeter * contourPerimeter)
            if circularity < minCircularity:
                continue

            (circleX, circleY), candidateRadius = cv2.minEnclosingCircle(candidateContour)
            if candidateRadius <= 0:
                continue
            circleMask = np.zeros(colorMask.shape, dtype=np.uint8)
            cv2.circle(
                circleMask,
                (int(round(circleX)), int(round(circleY))),
                int(round(candidateRadius)),
                255,
                -1,
            )
            circleAreaPx = cv2.countNonZero(circleMask)
            if circleAreaPx == 0:
                continue
            coloredAreaPx = cv2.countNonZero(cv2.bitwise_and(colorMask, circleMask))
            circleFillRatio = coloredAreaPx / circleAreaPx
            if circleFillRatio < minCircleFillRatio:
                continue

            score = contourArea * (circularity ** 3) * (circleFillRatio ** 3)
            if bestScore is None or score > bestScore:
                bestScore = score
                bestContour = candidateContour
                bestCircle = ((circleX, circleY), candidateRadius)

        if bestContour is not None:
            contourMoments = cv2.moments(bestContour)
            if contourMoments["m00"] != 0:
                centerX = int(contourMoments["m10"] / contourMoments["m00"])
                centerY = int(contourMoments["m01"] / contourMoments["m00"])
                detectedPose = (centerX, centerY, 0, handoffRzDeg)

    # ---- Annotated preview frame ---------------------------------------------
    annotated = cv2.GaussianBlur(bgrFrame.copy(), previewBlurKernel, 0)
    cv2.drawContours(annotated, maskContours, -1, (0, 255, 255), 1)

    if bestCircle is not None:
        (circleX, circleY), circleRadius = bestCircle
        cv2.circle(
            annotated,
            (int(round(circleX)), int(round(circleY))),
            int(round(circleRadius)),
            (255, 0, 255),
            2,
        )

    if centerX is not None and centerY is not None:
        cv2.circle(annotated, (centerX, centerY), 5, (0, 0, 255), -1)

    hudLines = [f"detector=bowl  color={targetColor or 'any'}"]
    if detectedPose is not None:
        hudLines.append(f"cam(px): x={detectedPose[0]} y={detectedPose[1]} rz={detectedPose[3]}")
    else:
        hudLines.append("cam(px): no bowl detected")
    drawHud(annotated, hudLines)

    return detectedPose, annotated, {"color": targetColor, "samples": 1}

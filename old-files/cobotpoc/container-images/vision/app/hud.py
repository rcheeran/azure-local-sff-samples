"""Translucent HUD overlay -- direct port of drawHud() from cobotpoc.py."""

import cv2


def drawHud(frame, lines, originY: int = 22) -> None:
    """Draw a translucent black box at top-left containing white text lines."""
    if frame is None or not lines:
        return
    pad = 6
    fontFace = cv2.FONT_HERSHEY_SIMPLEX
    fontScale = 0.5
    lineHeight = 18
    boxW = 0
    for line in lines:
        (w, _h), _b = cv2.getTextSize(line, fontFace, fontScale, 1)
        boxW = max(boxW, w)
    boxW += pad * 2
    boxH = lineHeight * len(lines) + pad
    overlay = frame.copy()
    cv2.rectangle(
        overlay,
        (0, originY - lineHeight),
        (boxW, originY - lineHeight + boxH),
        (0, 0, 0),
        -1,
    )
    cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)
    for idx, line in enumerate(lines):
        y = originY + idx * lineHeight
        cv2.putText(
            frame,
            line,
            (pad, y),
            fontFace,
            fontScale,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )

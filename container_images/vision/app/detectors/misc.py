"""Open-vocabulary detector -- port of lookForMisc() from cobotpoc.py.

Uses HuggingFace OWL-ViT (google/owlvit-base-patch32). The processor and
model are loaded once at server startup and passed in by the caller.
"""

from typing import Optional, Tuple

import cv2
import numpy as np

from app.hud import drawHud


_DETECTION_THRESHOLD = 0.10
_GRASP_RZ_DEG = 45


def detectMisc(
    bgrFrame: np.ndarray,
    targetText: str,
    owlProcessor,
    owlModel,
) -> Tuple[Optional[Tuple[int, int, int, int]], np.ndarray, dict]:
    """Detect an open-vocabulary object in a single BGR frame."""
    # Lazy import so this module loads even if torch isn't present in tests.
    import torch  # type: ignore

    queryText = (targetText or "").strip() or "object"

    frameH, frameW = bgrFrame.shape[:2]
    rgbFrame = cv2.cvtColor(bgrFrame, cv2.COLOR_BGR2RGB)

    owlInputs = owlProcessor(text=[[queryText]], images=rgbFrame, return_tensors="pt").to(owlModel.device)
    with torch.inference_mode():
        owlOutputs = owlModel(**owlInputs)

    targetSizes = torch.tensor([rgbFrame.shape[:2]], dtype=torch.float32)
    detections = owlProcessor.post_process_object_detection(
        outputs=owlOutputs,
        target_sizes=targetSizes,
        threshold=_DETECTION_THRESHOLD,
    )[0]

    scores = detections["scores"]
    boxes = detections["boxes"]

    detectedPose = None
    bestScore = None
    bestBox = None

    if len(scores) > 0:
        topIndex = int(torch.argmax(scores).item())
        bestScore = float(scores[topIndex].item())
        x1, y1, x2, y2 = [int(round(v)) for v in boxes[topIndex].tolist()]
        x1, x2 = max(0, min(frameW - 1, x1)), max(0, min(frameW - 1, x2))
        y1, y2 = max(0, min(frameH - 1, y1)), max(0, min(frameH - 1, y2))
        if x2 > x1 and y2 > y1:
            centerX = (x1 + x2) // 2
            centerY = (y1 + y2) // 2
            detectedPose = (centerX, centerY, 0, _GRASP_RZ_DEG)
            bestBox = (x1, y1, x2, y2)

    annotated = bgrFrame.copy()
    if bestBox is not None:
        x1, y1, x2, y2 = bestBox
        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
    if detectedPose is not None:
        cv2.circle(annotated, (detectedPose[0], detectedPose[1]), 6, (0, 0, 255), -1)

    hudLines = [f'detector=misc  query="{queryText}"']
    if detectedPose is not None:
        hudLines.append(
            f"cam(px): x={detectedPose[0]} y={detectedPose[1]} rz={detectedPose[3]}  score={bestScore:.2f}"
        )
    else:
        hudLines.append("cam(px): no detection")
    drawHud(annotated, hudLines)

    return detectedPose, annotated, {"samples": 1, "score": bestScore}

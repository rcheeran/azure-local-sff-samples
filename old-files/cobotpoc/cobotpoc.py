from contextlib import contextmanager
import os
import sys
import time
import threading
import torch
import json
import cv2
import numpy as np
import scipy as sp
import glob
import sounddevice as sd
from rich.console import Console
from rich.theme import Theme

PURPLE = "#D388FF"
BLUE = "#52BBFF"
RICH = Console(
    theme = Theme(
        {
            "json.brace": f"bold {BLUE}",
            "json.key": f"bold {PURPLE}",
            "json.str": f"bold white"
        }
    )
)

@contextmanager
def printVerbosely(codeBlockDescription):
    """Print to console before/after block, report outcome and duration"""
    RICH.print(f"[bold white]{codeBlockDescription}...[/bold white]")
    start = time.time()
    success = True

    try:
        yield
    except Exception:
        success = False
        raise

    finally:
        elapsed = time.time() - start
        if success:
            RICH.print(f"[bold green]✔ {codeBlockDescription} succeeded in {elapsed:.2f}s[/bold green]")
        else:
            RICH.print(f"[bold red]✗ {codeBlockDescription} failed after {elapsed:.2f}s[/bold red]")

@contextmanager
def suppressStdOut():
    """Suppress stdout and stderr at the OS file descriptor level (catches C++ output from OpenCV, NeMo, MediaPipe, etc)"""
    stdout_fd = sys.stdout.fileno()
    stderr_fd = sys.stderr.fileno()
    with open(os.devnull, 'w') as devnull:
        old_stdout = os.dup(stdout_fd)
        old_stderr = os.dup(stderr_fd)
        os.dup2(devnull.fileno(), stdout_fd)
        os.dup2(devnull.fileno(), stderr_fd)
        try:
            yield
        finally:
            os.dup2(old_stdout, stdout_fd)
            os.dup2(old_stderr, stderr_fd)
            os.close(old_stdout)
            os.close(old_stderr)

###########################
### MICROPHONE & SPEECH ###
###########################

def listMicrophones():
    """Return available microphones as dictionaries of index, name, channels, default_samplerate"""
    devices = sd.query_devices()

    if not devices:
        return None

    allMicrophones = []

    for index, device in enumerate(devices):
        if device["max_input_channels"] <= 0:
            continue
        if device["max_input_channels"] > 8:
            continue
        if not device.get("default_samplerate"):
            continue

        allMicrophones.append({
            "Name": device["name"],
            "Index": index,
            "Channels": device["max_input_channels"],
            "SampleRate": device["default_samplerate"]
        })

    return allMicrophones or None

def selectBestMicrophone(allMicrophones):
    """Given list of microphones, select one, preferring USB and/or single-channel"""

    if not allMicrophones:
        return None

    usbMics = [mic for mic in allMicrophones if "usb" in mic["Name"].lower()]
    if usbMics:
        return usbMics[0]

    monoMics = [mic for mic in allMicrophones if mic["Channels"] == 1]
    if monoMics:
        return monoMics[0]

    return allMicrophones[0]

def makeSpeechCallback(inputSampleRate, targetSampleRate=16000, minTalkingThreshold=5, maxSilenceThreshold=5, quietThreshold=0.01):
    """Build a non-blocking microphone callback that segments utterances by speech followed by silence."""
    audioBuffer = []
    silenceCount = 0
    talkingCount = 0

    def callback(indata, frames, callback_time, status):
        global newestUtterance
        nonlocal audioBuffer, silenceCount, talkingCount

        def isTooQuiet(chunk, threshold=quietThreshold):
            rms = np.sqrt(np.mean(chunk ** 2))
            return rms < threshold

        if status:
            return

        audioChunk = indata[:, 0].astype(np.float32)
        audioChunkResampled = sp.signal.resample_poly(audioChunk, up=targetSampleRate, down=inputSampleRate).astype(np.float32)
        audioBuffer.append(audioChunkResampled)

        if isTooQuiet(audioChunkResampled):
            silenceCount += 1
        else:
            talkingCount += 1
            silenceCount = 0

        if silenceCount < maxSilenceThreshold:
            return

        if talkingCount >= minTalkingThreshold and len(audioBuffer) >= (minTalkingThreshold + maxSilenceThreshold):
            newestUtterance = np.concatenate(audioBuffer, axis=0)

        audioBuffer = []
        silenceCount = 0
        talkingCount = 0

    return callback

def transcribeSpeech(speechModel, audio):
    """Transcribe one utterance audio buffer into text."""
    if speechModel is None or audio is None or len(audio) == 0:
        return ""

    with suppressStdOut():
        transcription = speechModel.transcribe([audio], verbose=False)

    if not transcription:
        return ""

    firstResult = transcription[0]
    if hasattr(firstResult, "text"):
        return firstResult.text.strip()
    if isinstance(firstResult, str):
        return firstResult.strip()

    return str(firstResult).strip()

def startSpeechPipelineWorker():
    """Start a background worker that transcribes utterances and parses them into robot commands."""
    stopEvent = threading.Event()

    def worker():
        global newestUtterance
        global newestToolCall

        while not stopEvent.is_set():
            utteranceAudio = newestUtterance
            newestUtterance = None

            if utteranceAudio is None:
                time.sleep(0.05)
                continue

            userText = transcribeSpeech(SPEECH_MODEL, utteranceAudio)
            if not userText:
                continue

            toolCallJSON = interpretWithLLM(userText)
            RICH.print() # Empty line
            RICH.print(f'[bold {PURPLE}]HEARD:[/bold {PURPLE}] [bold white]"{userText}"[/bold white]')

            newestToolCall = {
                "userText": userText,
                "toolCallJSON": toolCallJSON,
            }

    workerThread = threading.Thread(target=worker, name="speech-pipeline", daemon=True)
    workerThread.start()
    return stopEvent, workerThread

#######################
### CAMERA & VISION ###
#######################

def listCameras():
    """Return available cameras as dictionaries of index, width, height"""
    allCameras = []
    videoDevices = glob.glob('/dev/video*')

    for device in videoDevices:
        index = int(device.replace('/dev/video', ''))
        with suppressStdOut():
            camera = cv2.VideoCapture(index, cv2.CAP_V4L2)
            if camera.isOpened():
                success, frame = camera.read()
                if success and frame is not None:
                    cameraInfo = {
                        "Index" : index,
                        "Width" : int(camera.get(cv2.CAP_PROP_FRAME_WIDTH)),
                        "Height": int(camera.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    }
                    allCameras.append(cameraInfo)
                camera.release()

    return allCameras or None

def selectBestCamera(allCameras):
    """Given list of cameras, select one, preferring highest resolution"""

    if not allCameras:
        return None

    sortedByRes = sorted(allCameras, key=lambda camera: camera["Width"] * camera["Height"], reverse=True)
    if sortedByRes:
        return sortedByRes[0]

    return allCameras[0]

def lookForCube(description="", preview=False):
    """Detect a cube, optionally colored, and return its center coordinates and orientation

    The detector works by:
        applying HSV threshold mask to each frame
        contouring and requiring minimum area, near-square aspect ratio, and high fill ratio
        then averages the middle 3 of 5 frames to reduce noise

    Arguments:
        description: text like "red cube" from which color will be extracted if known
        preview: If True, displays a final debug frame with detected contours, fitted rectangle, centroid, and major-axis overlay

    Returns:
        Tuple (x, y, z, rz) in camera pixel space, or None if no detection
    """
    maxAttempts = 5
    frameFlushCount = 3
    blurKernel = (5, 5)
    previewBlurKernel = (3, 3)
    morphKernelSize = (7, 7)
    morphIterations = 2
    minContourArea = 400 # Minimum 20px x 20px
    minAspectRatio = 0.70
    minRectFillRatio = 0.80
    axisLengthPx = 100

    camera = CAMERA

    # Be picky about hue (H) but tolerate most saturation (S from ~100 to 255) and value (V from ~50 to 255)
    hsvRangesByColor = {
        "red": [(np.array([164, 65, 65]), np.array([180, 255, 255])), (np.array([0, 65, 65]), np.array([10, 255, 255]))],
        "green": [(np.array([66, 100, 25]), np.array([94, 255, 255]))],
        "blue": [(np.array([95, 130, 50]), np.array([123, 255, 255]))],
        "yellow": [(np.array([18, 50, 65]), np.array([44, 255, 255]))],
    }

    descriptionText = (description or "").lower()
    targetColor = None
    for knownColor in ["red", "green", "blue", "yellow"]:
        if knownColor in descriptionText:
            targetColor = knownColor
            break
    if targetColor is not None:
        targetHsvRanges = hsvRangesByColor[targetColor]
    else:
        targetHsvRanges = []
        for hsvRanges in hsvRangesByColor.values():
            targetHsvRanges.extend(hsvRanges)

    lastFrameUsed = None
    bestContour = None
    bestRect = None
    rectAngleDeg = None
    rectWidth = None
    rectHeight = None
    centerX = None
    centerY = None
    detectedPose = None
    poseSamples = []
    lastMaskContours = []

    def trimmedMean(values):
        """Given list of values, remove highest/lower outliers and average the rest"""
        if len(values) < 3:
            return None
        sortedValues = sorted(values)
        trimmedValues = sortedValues[1:-1] if len(sortedValues) >= 5 else sortedValues
        return sum(trimmedValues) / len(trimmedValues)

    # Repeat the whole masking, contouring, filtering, etc process multiple times to average multiple camera frames
    for _ in range(maxAttempts):

        # Discard a few buffered frames to avoid using stale images
        for _ in range(frameFlushCount):
            camera.read()

        success, bgrFrame = camera.read()
        if not success:
            continue

        lastFrameUsed = bgrFrame

        bgrFrame = cv2.GaussianBlur(bgrFrame, blurKernel, 0)

        hsvFrame = cv2.cvtColor(bgrFrame, cv2.COLOR_BGR2HSV)

        colorMask = np.zeros(hsvFrame.shape[:2], dtype=np.uint8)
        for lowerBound, upperBound in targetHsvRanges:
            colorMask = cv2.bitwise_or(colorMask, cv2.inRange(hsvFrame, lowerBound, upperBound))

        morphKernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, morphKernelSize)
        colorMask = cv2.morphologyEx(colorMask, cv2.MORPH_CLOSE, morphKernel, iterations=morphIterations)

        maskContours, _ = cv2.findContours(colorMask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        lastMaskContours = maskContours
        bestContour = None
        bestRect = None
        rectAngleDeg = None
        rectWidth = None
        rectHeight = None
        centerX = None
        centerY = None
        detectedPose = None

        # Score and select the best contour candidate from this frame
        if maskContours:
            bestScore = None
            for candidateContour in maskContours:

                # Step 1: reject trivially small contours
                contourArea = cv2.contourArea(candidateContour)
                if contourArea < minContourArea:
                    continue

                # Step 2: fit a rotated rectangle to assess shape
                contourRect = cv2.minAreaRect(candidateContour)
                (_, _), (candidateWidth, candidateHeight), _ = contourRect
                if candidateWidth <= 0 or candidateHeight <= 0:
                    continue

                # Step 3: require near-square aspect ratio to suppress elongated detections
                rectAspectRatio = min(candidateWidth, candidateHeight) / max(candidateWidth, candidateHeight)
                if rectAspectRatio < minAspectRatio:
                    continue

                # Step 4: measure how fully the expected color fills the fitted rectangle to penalize empty areas (e.g., circle, donut)
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

                # Step 5: combine area, squareness, and fill quality into one score
                score = contourArea * (rectAspectRatio ** 3) * (rectFillRatio ** 3)
                if bestScore is None or score > bestScore:
                    bestScore = score
                    bestContour = candidateContour

            # Convert the winning contour into output centerX, centerY and orientation
            if bestContour is not None:
                contourMoments = cv2.moments(bestContour)
                if contourMoments["m00"] != 0:
                    centerX = int(contourMoments["m10"] / contourMoments["m00"])
                    centerY = int(contourMoments["m01"] / contourMoments["m00"])
                    centerZ = 0

                    bestRect = cv2.minAreaRect(bestContour)
                    (_, _), (rectWidth, rectHeight), rectAngleDeg = bestRect
                    graspRzDeg = int(rectAngleDeg % 90.0) # Gasp angle is periodic every 90 degs for cube
                    detectedPose = (centerX, centerY, centerZ, graspRzDeg)
                    poseSamples.append(
                        {
                            "x": centerX,
                            "y": centerY,
                            "rectAngleDeg": rectAngleDeg,
                            "width": rectWidth,
                            "height": rectHeight,
                        }
                    )

    if len(poseSamples) >= 2:
        # Handle multiple cubes by clustering detections to stably pick one and discard the rest
        clusterThresholdPx = 30
        def sampleDist(a, b):
            return ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5

        anchor = max(poseSamples, key=lambda s: sum(1 for o in poseSamples if sampleDist(s, o) <= clusterThresholdPx))
        poseSamples = [s for s in poseSamples if sampleDist(s, anchor) <= clusterThresholdPx]

    # Aggregate multi-frame detections into one stabilized pose estimate
    averagedRect = None
    if len(poseSamples) >= 3:
        avgCenterX = trimmedMean([sample["x"] for sample in poseSamples])
        avgCenterY = trimmedMean([sample["y"] for sample in poseSamples])
        avgRectAngleDeg = trimmedMean([sample["rectAngleDeg"] for sample in poseSamples])
        avgRectWidth = trimmedMean([sample["width"] for sample in poseSamples])
        avgRectHeight = trimmedMean([sample["height"] for sample in poseSamples])

        if None not in (avgCenterX, avgCenterY, avgRectAngleDeg, avgRectWidth, avgRectHeight):
            centerX = int(round(avgCenterX))
            centerY = int(round(avgCenterY))
            rectAngleDeg = avgRectAngleDeg
            rectWidth = avgRectWidth
            rectHeight = avgRectHeight
            bestRect = ((avgCenterX, avgCenterY), (avgRectWidth, avgRectHeight), avgRectAngleDeg)
            averagedRect = bestRect
            centerZ = 0
            graspRzDeg = int(rectAngleDeg % 90.0)
            detectedPose = (centerX, centerY, centerZ, graspRzDeg)

    # Render one final preview with contours and the selected/averaged geometry overlay
    if preview:
        if lastFrameUsed is None:
            return detectedPose

        finalPreviewFrame = cv2.GaussianBlur(lastFrameUsed.copy(), previewBlurKernel, 0)
        cv2.drawContours(finalPreviewFrame, lastMaskContours, -1, (0, 255, 255), 1)

        rectForPreview = averagedRect if averagedRect is not None else bestRect
        if rectForPreview is not None:
            rectCorners = cv2.boxPoints(rectForPreview)
            rectCorners = np.intp(rectCorners)
            cv2.drawContours(finalPreviewFrame, [rectCorners], 0, (255, 0, 255), 2)

        if centerX is not None and centerY is not None:
            cv2.circle(finalPreviewFrame, (centerX, centerY), 5, (0, 0, 255), -1)
            if rectAngleDeg is not None and rectWidth is not None and rectHeight is not None:
                majorAxisDeg = rectAngleDeg if rectWidth >= rectHeight else rectAngleDeg + 90
                halfAxisPx = axisLengthPx / 2
                majorAxisRad = np.deg2rad(majorAxisDeg)
                axisDx = int(np.cos(majorAxisRad) * halfAxisPx)
                axisDy = int(np.sin(majorAxisRad) * halfAxisPx)
                axisStart = (centerX - axisDx, centerY - axisDy)
                axisEnd = (centerX + axisDx, centerY + axisDy)
                cv2.line(finalPreviewFrame, axisStart, axisEnd, (255, 255, 0), 2)

        if detectedPose is not None:
            print(f"Cube detected: x={detectedPose[0]}, y={detectedPose[1]}, rz={detectedPose[3]}")

        previewWindowName = f"lookForCube preview: {targetColor or 'any'}"
        cv2.namedWindow(previewWindowName, cv2.WINDOW_NORMAL)
        cv2.imshow(previewWindowName, finalPreviewFrame)
        cv2.waitKey(0)
        cv2.destroyWindow(previewWindowName)

    return detectedPose

def lookForBowl(description="", preview=False):
    """Detect a bowl-like round object and return its center coordinates (orientation doesn't matter)

    The detector works by:
        applying HSV threshold mask to each frame
        contouring and requiring minimum area, round shape, and high circle fill ratio
        then averages the middle 3 of 5 frames to reduce noise

    Arguments:
        description: text like "green bowl" from which color will be extracted if known. Includes white as a valid color
        preview: If True, displays a final debug frame with detected contours, fitted circle, and centroid

    Returns:
        Tuple (x, y, z, rz) in camera pixel space, or None if no detection
    """
    maxAttempts = 5
    frameFlushCount = 3
    blurKernel = (5, 5)
    previewBlurKernel = (3, 3)
    morphKernelSize = (7, 7)
    morphIterations = 2
    minContourArea = 900 # Minimum 30px by 30px
    minCircularity = 0.65
    minCircleFillRatio = 0.75

    camera = CAMERA

    # Be picky about hue (H) but tolerate most saturation (S from ~100 to 255) and value (V from ~50 to 255)
    hsvRangesByColor = {
        "red": [(np.array([164, 65, 65]), np.array([180, 255, 255])), (np.array([0, 65, 65]), np.array([10, 255, 255]))],
        "green": [(np.array([66, 100, 25]), np.array([94, 255, 255]))],
        "blue": [(np.array([95, 130, 50]), np.array([123, 255, 255]))],
        "yellow": [(np.array([18, 50, 65]), np.array([44, 255, 255]))],
        "white": [(np.array([0, 0, 180]), np.array([180, 50, 255]))],
    }

    descriptionText = (description or "").lower()
    targetColor = None
    for knownColor in ["red", "green", "blue", "yellow", "white"]:
        if knownColor in descriptionText:
            targetColor = knownColor
            break
    if targetColor is not None:
        targetHsvRanges = hsvRangesByColor[targetColor]
    else:
        targetHsvRanges = []
        for hsvRanges in hsvRangesByColor.values():
            targetHsvRanges.extend(hsvRanges)

    lastFrameUsed = None
    bestContour = None
    bestCircle = None
    centerX = None
    centerY = None
    detectedPose = None
    poseSamples = []
    lastMaskContours = []

    def trimmedMean(values):
        """Given list of values, remove highest/lower outliers and average the rest"""
        if len(values) < 3:
            return None
        sortedValues = sorted(values)
        trimmedValues = sortedValues[1:-1] if len(sortedValues) >= 5 else sortedValues
        return sum(trimmedValues) / len(trimmedValues)

    # Repeat the whole masking, contouring, filtering, etc process multiple times to average multiple camera frames
    for _ in range(maxAttempts):

        # Discard a few buffered frames to avoid using stale images
        for _ in range(frameFlushCount):
            camera.read()

        success, bgrFrame = camera.read()
        if not success:
            continue

        lastFrameUsed = bgrFrame

        bgrFrame = cv2.GaussianBlur(bgrFrame, blurKernel, 0)

        hsvFrame = cv2.cvtColor(bgrFrame, cv2.COLOR_BGR2HSV)

        colorMask = np.zeros(hsvFrame.shape[:2], dtype=np.uint8)
        for lowerBound, upperBound in targetHsvRanges:
            colorMask = cv2.bitwise_or(colorMask, cv2.inRange(hsvFrame, lowerBound, upperBound))

        morphKernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, morphKernelSize)
        colorMask = cv2.morphologyEx(colorMask, cv2.MORPH_CLOSE, morphKernel, iterations=morphIterations)

        maskContours, _ = cv2.findContours(colorMask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        lastMaskContours = maskContours
        bestContour = None
        bestCircle = None
        centerX = None
        centerY = None
        detectedPose = None

        # Score and select the best round contour candidate from this frame
        if maskContours:
            bestScore = None
            for candidateContour in maskContours:

                # Step 1: reject trivially small contours
                contourArea = cv2.contourArea(candidateContour)
                if contourArea < minContourArea:
                    continue

                # Step 2: reject non-round contours using circularity
                contourPerimeter = cv2.arcLength(candidateContour, True)
                if contourPerimeter <= 0:
                    continue
                circularity = (4.0 * np.pi * contourArea) / (contourPerimeter * contourPerimeter)
                if circularity < minCircularity:
                    continue

                # Step 3: measure how fully the expected color fills a fitted circle to penalize hollow shapes
                (circleX, circleY), candidateRadius = cv2.minEnclosingCircle(candidateContour)
                if candidateRadius <= 0:
                    continue
                circleMask = np.zeros(colorMask.shape, dtype=np.uint8)
                cv2.circle(circleMask, (int(round(circleX)), int(round(circleY))), int(round(candidateRadius)), 255, -1)
                circleAreaPx = cv2.countNonZero(circleMask)
                if circleAreaPx == 0:
                    continue
                coloredAreaPx = cv2.countNonZero(cv2.bitwise_and(colorMask, circleMask))
                circleFillRatio = coloredAreaPx / circleAreaPx
                if circleFillRatio < minCircleFillRatio:
                    continue

                # Step 4: combine area, circularity, and fill quality into one score
                score = contourArea * (circularity ** 3) * (circleFillRatio ** 3)
                if bestScore is None or score > bestScore:
                    bestScore = score
                    bestContour = candidateContour
                    bestCircle = ((circleX, circleY), candidateRadius)

            # Convert the winning contour into output centerX, centerY
            if bestContour is not None:
                contourMoments = cv2.moments(bestContour)
                if contourMoments["m00"] != 0:
                    centerX = int(contourMoments["m10"] / contourMoments["m00"])
                    centerY = int(contourMoments["m01"] / contourMoments["m00"])
                    centerZ = 0
                    graspRzDeg = 0 # Round objects have no orientation
                    detectedPose = (centerX, centerY, centerZ, graspRzDeg)
                    poseSamples.append(
                        {
                            "x": centerX,
                            "y": centerY,
                        }
                    )

    if len(poseSamples) >= 2:
        # Handle multiple cubes by clustering detections to stably pick one and discard the rest
        clusterThresholdPx = 30

        def sampleDist(a, b):
            return ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5

        anchor = max(poseSamples, key=lambda s: sum(1 for o in poseSamples if sampleDist(s, o) <= clusterThresholdPx))
        poseSamples = [s for s in poseSamples if sampleDist(s, anchor) <= clusterThresholdPx]

    # Aggregate multi-frame detections into one stabilized pose estimate
    if len(poseSamples) >= 3:
        avgCenterX = trimmedMean([sample["x"] for sample in poseSamples])
        avgCenterY = trimmedMean([sample["y"] for sample in poseSamples])

        if None not in (avgCenterX, avgCenterY):
            centerX = int(round(avgCenterX))
            centerY = int(round(avgCenterY))
            centerZ = 0
            graspRzDeg = 45 # Fixed handoff orientation for the robot gripper
            detectedPose = (centerX, centerY, centerZ, graspRzDeg)

    # Render one final preview with contours and the selected/averaged geometry overlay
    if preview:
        if lastFrameUsed is None:
            return detectedPose

        finalPreviewFrame = cv2.GaussianBlur(lastFrameUsed.copy(), previewBlurKernel, 0)
        cv2.drawContours(finalPreviewFrame, lastMaskContours, -1, (0, 255, 255), 1)

        if bestCircle is not None:
            (circleX, circleY), circleRadius = bestCircle
            cv2.circle(finalPreviewFrame, (int(round(circleX)), int(round(circleY))), int(round(circleRadius)), (255, 0, 255), 2)

        if centerX is not None and centerY is not None:
            cv2.circle(finalPreviewFrame, (centerX, centerY), 5, (0, 0, 255), -1)

        if detectedPose is not None:
            print(f"Bowl detected: x={detectedPose[0]}, y={detectedPose[1]}, rz={detectedPose[3]}")

        previewWindowName = f"lookForBowl preview: {targetColor or 'any'}"
        cv2.namedWindow(previewWindowName, cv2.WINDOW_NORMAL)
        cv2.imshow(previewWindowName, finalPreviewFrame)
        cv2.waitKey(0) # wait until a key is pressed
        cv2.destroyWindow(previewWindowName)

    return detectedPose

def lookForHand(preview=False):
    """Detect a hand and return its center coordinates using HandLandmarker model

    Arguments:
        preview: If True, displays a debug frame with hand landmarks and center marker

    Returns:
        Tuple (x, y, z, rz) in camera pixel space where z=0 and rz is a fixed handoff orientation, or None if no hand detected
    """
    maxAttempts = 5
    frameFlushCount = 3
    handCount = 1
    minDetectionConfidence = 0.6
    minTrackingConfidence = 0.5
    handoffRz = 45

    camera = CAMERA

    detectedPose = None
    lastFrameUsed = None
    lastDrawnFrame = None

    # Try up to 5 single-frame detections, mainly to handle motion/transient misses, return immediately on first successful hand detection
    for _ in range(maxAttempts):
        # Flush stale buffered frames
        for _ in range(frameFlushCount):
            camera.grab()

        success, frame = camera.read()
        if not success:
            continue

        lastFrameUsed = frame
        frameH, frameW = frame.shape[:2]
        frameRgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mpImage = mp.Image(image_format=mp.ImageFormat.SRGB, data=frameRgb)
        results = HAND_LANDMARKER.detect(mpImage)

        if not results.hand_landmarks:
            continue

        handLandmarks = results.hand_landmarks[0]

        # Average the wrist (0) and four knuckle bases (5, 9, 13, 17) to get roughly center of palm
        palmIndices = [0, 5, 9, 13, 17]
        pixelX = int(sum(handLandmarks[i].x for i in palmIndices) / len(palmIndices) * frameW)
        pixelY = int(sum(handLandmarks[i].y for i in palmIndices) / len(palmIndices) * frameH)
        pixelZ = 0
        detectedPose = (pixelX, pixelY, pixelZ, handoffRz)

        if preview:
            lastDrawnFrame = frame.copy()
            connections = _mpVision.HandLandmarksConnections.HAND_CONNECTIONS
            for lm in handLandmarks:
                lx, ly = int(lm.x * frameW), int(lm.y * frameH)
                cv2.circle(lastDrawnFrame, (lx, ly), 4, (0, 255, 0), -1)
            for connection in connections:
                startLm = handLandmarks[connection.start]
                endLm = handLandmarks[connection.end]
                cv2.line(lastDrawnFrame,
                         (int(startLm.x * frameW), int(startLm.y * frameH)),
                         (int(endLm.x * frameW), int(endLm.y * frameH)),
                         (0, 255, 0), 1)
            cv2.circle(lastDrawnFrame, (pixelX, pixelY), 8, (0, 0, 255), -1)

        break

    if preview:
        if lastDrawnFrame is not None and detectedPose is not None:
            cx, cy = detectedPose[0], detectedPose[1]
            cv2.circle(lastDrawnFrame, (cx, cy), 5, (0, 0, 255), -1)
            print(f"Hand detected: x={cx}, y={cy}, rz={detectedPose[3]}")
        previewWindowName = "lookForHand preview"
        cv2.namedWindow(previewWindowName, cv2.WINDOW_NORMAL)
        cv2.imshow(previewWindowName, lastDrawnFrame if lastDrawnFrame is not None else lastFrameUsed)
        cv2.waitKey(0)
        cv2.destroyWindow(previewWindowName)

    return detectedPose

def lookForMisc(description, preview=False):
    """Detect an open-vocabulary object using OWL-ViT model and return its center coordinates"""
    maxAttempts = 3
    frameFlushCount = 3
    detectionThreshold = 0.10
    graspRzDeg = 45

    if OWL_VIT_PROCESSOR is None or OWL_VIT_MODEL_INSTANCE is None:
        return None

    camera = CAMERA
    queryText = (description or "").strip() or "object"

    bestDetectedPose = None
    bestScore = None
    bestBox = None
    lastFrameUsed = None

    for _ in range(maxAttempts):
        for _ in range(frameFlushCount):
            camera.grab()

        success, bgrFrame = camera.read()
        if not success:
            continue

        lastFrameUsed = bgrFrame
        frameH, frameW = bgrFrame.shape[:2]
        rgbFrame = cv2.cvtColor(bgrFrame, cv2.COLOR_BGR2RGB)

        owlInputs = OWL_VIT_PROCESSOR(text=[[queryText]], images=rgbFrame, return_tensors="pt")
        with torch.inference_mode():
            owlOutputs = OWL_VIT_MODEL_INSTANCE(**owlInputs)

        targetSizes = torch.tensor([rgbFrame.shape[:2]], dtype=torch.float32)
        detections = OWL_VIT_PROCESSOR.post_process_object_detection(
            outputs=owlOutputs,
            target_sizes=targetSizes,
            threshold=detectionThreshold,
        )[0]

        scores = detections["scores"]
        boxes = detections["boxes"]
        if len(scores) == 0:
            continue

        topIndex = int(torch.argmax(scores).item())
        score = float(scores[topIndex].item())
        x1, y1, x2, y2 = [int(round(v)) for v in boxes[topIndex].tolist()]
        x1, x2 = max(0, min(frameW - 1, x1)), max(0, min(frameW - 1, x2))
        y1, y2 = max(0, min(frameH - 1, y1)), max(0, min(frameH - 1, y2))
        if x2 <= x1 or y2 <= y1:
            continue

        centerX = (x1 + x2) // 2
        centerY = (y1 + y2) // 2
        detectedPose = (centerX, centerY, 0, graspRzDeg)

        if bestScore is None or score > bestScore:
            bestScore = score
            bestDetectedPose = detectedPose
            bestBox = (x1, y1, x2, y2)

    if preview and lastFrameUsed is not None:
        previewFrame = lastFrameUsed.copy()
        if bestBox is not None:
            x1, y1, x2, y2 = bestBox
            cv2.rectangle(previewFrame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        if bestDetectedPose is not None:
            cx, cy = bestDetectedPose[0], bestDetectedPose[1]
            cv2.circle(previewFrame, (cx, cy), 6, (0, 0, 255), -1)
            print(f"Misc object detected: x={cx}, y={cy}, rz={bestDetectedPose[3]}, score={bestScore:.3f}")

        previewWindowName = f"lookForMisc preview: {queryText}"
        cv2.namedWindow(previewWindowName, cv2.WINDOW_NORMAL)
        cv2.imshow(previewWindowName, previewFrame)
        cv2.waitKey(0)
        cv2.destroyWindow(previewWindowName)

    return bestDetectedPose

def getTargetCoordsFromVision(target):
    """Given JSON-like object with targetText and targetType, route to best vision and return safe robot coords, or None."""

    def transformCoordsFromCameraToRobot(coords):
        """Transform coordinates from camera frame to robot base frame using homography and various offsets."""
        if coords is None:
            return None
        inputX, inputY, inputZ, inputRz = coords
        # Position: compute homography using values from calibrate.py
        robotCorners = np.array(
            [
                [-50, 200],
                [200, 200],
                [200, -200],
                [-50, -200],
            ],
            dtype=np.float32,
        )
        cameraCorners = np.array(
            [
                [486, 62],
                [485, 298],
                [116, 306],
                [104, 73],
            ],
            dtype=np.float32,
        )
        homography = cv2.getPerspectiveTransform(cameraCorners, robotCorners)
        point = np.array([[[float(inputX), float(inputY)]]], dtype=np.float32)
        mapped = cv2.perspectiveTransform(point, homography)
        outputX, outputY = float(mapped[0, 0, 0]), float(mapped[0, 0, 1])
        # Angle: shift 45 degrees, measured empirically
        outputRz = -(inputRz - 45)
        # Offset: necessary because gripper head is about 1cm ahead of the wrist rotation axis (ugh!)
        outputX += -10 * np.sin(np.deg2rad(outputRz + 45))
        outputY += 10 * np.cos(np.deg2rad(outputRz + 45))
        # Do nothing with Z, just leave 0
        outputZ = inputZ
        # Return
        return outputX, outputY, outputZ, outputRz

    def verifyCoordsAreSafe(coords):
        """Verify coordinates are within safe reachable area: between 100 mm and 280 mm distance from base, and x >= -100 mm."""
        if coords is None:
            return False
        inputX, inputY, inputZ, inputRz = coords
        if inputX < -100:
            return False
        distance = (inputX ** 2 + inputY ** 2) ** 0.5
        if distance < 100 or distance > 280:
            return False
        return True

    if not isinstance(target, dict):
        return None

    targetText = target.get("targetText", "")
    targetType = (target.get("targetType") or "misc").lower()

    # Route to the appropriate vision function based on targetType
    cameraCoords = None
    if targetType == "cube":
        cameraCoords = lookForCube(targetText)
    elif targetType == "bowl":
        cameraCoords = lookForBowl(targetText)
    elif targetType == "hand":
        cameraCoords = lookForHand()
    else:
        cameraCoords = lookForMisc(targetText)

    # Transform the camera coordinates into robot base frame coordinates
    robotCoords = transformCoordsFromCameraToRobot(cameraCoords)
    # Verify that the transformed robot coordinates are within the safe reachable area
    if not verifyCoordsAreSafe(robotCoords):
        return None
    return robotCoords

############################
### LANGUAGE & REASONING ###
############################

def interpretWithLLM(userText):
    EXTRACT_TOOL_CALL_SYSTEM_TEXT = (
        "Classify the command into one robot toolCall. "
        "Output JSON only with exactly one field: {\"tool\":\"<TOOL>\"}. "
        "Allowed tool values are exactly: pick, place, pickAndPlace, stop, none. "
        "Ignore filler words like hey, oh, uh, please, can you, and would you. "
        "Definitions: pick=pick object only. place=place object already held. pickAndPlace=pick object and place it in one command. stop=wait/stop/pause/hold. none=not a robot manipulation request. "
        "Decision rule 1: if command has both a concrete object phrase and a concrete destination phrase, output pickAndPlace. "
        "Decision rule 2: if object is only a pronoun (it/that/this) or explicitly already held, output place. "
        "Decision rule 3: person handoff phrases (give it to me, hand it to me, gimme, pass it to me, to me, my hand) are place. "
        "Decision rule 4: revision words (instead, actually, no wait, I changed my mind) do not change the action class by themselves; classify by the final requested action. "
        "Use none only when there is no meaningful robot manipulation action. "
        "Example: 'pick up the red cube' -> {\"tool\":\"pick\"}. "
        "Example: 'put it in the bowl' -> {\"tool\":\"place\"}. "
        "Example: 'give it to me' -> {\"tool\":\"place\"}. "
        "Example: 'give it to me instead' -> {\"tool\":\"place\"}. "
        "Example: 'ok please put the green cube in the green bowl' -> {\"tool\":\"pickAndPlace\"}. "
        "Example: 'please put the blue block in the red bowl' -> {\"tool\":\"pickAndPlace\"}. "
        "Example: 'move the cube to the plate' -> {\"tool\":\"pickAndPlace\"}. "
        "Example: 'actually wait' -> {\"tool\":\"stop\"}. "
        "Example: 'what time is it' -> {\"tool\":\"none\"}."
    )
    EXTRACT_PICK_SYSTEM_TEXT = (
        "The user's sentence indicates a PICK action. "
        "Your task is ONLY to extract WHAT OBJECT the robot should pick up. "
        "Do not extract the destination. Extract only the object being moved. "
        "Output JSON only with exactly two fields: {\"targetText\":\"<TEXT>\",\"targetType\":\"<TYPE>\"}. "
        "targetText must be the object words from the user sentence, including color and descriptors when present. "
        "targetType must be exactly one of: cube, bowl, hand, misc. "
        "Use cube for pickable objects like cube, block, brick, square, box, brick. "
        "Use bowl for pickable containers like bowl, plate, cup, dish, container. "
        "Use hand only if the object to pick is a hand (rare, for hand detection). "
        "Use misc only if none of cube, bowl, or hand apply to the object phrase. "
        "Critical rule: when both source object and destination appear, return the source object only. "
        "Example: 'pick up the red cube' -> {\"targetText\":\"red cube\",\"targetType\":\"cube\"}. "
        "Example: 'grab the yellow block and move it to the table' -> {\"targetText\":\"yellow block\",\"targetType\":\"cube\"}. "
        "Example: 'pick up the blue bowl' -> {\"targetText\":\"blue bowl\",\"targetType\":\"bowl\"}. "
        "Example: 'get the green container' -> {\"targetText\":\"green container\",\"targetType\":\"bowl\"}."
    )
    EXTRACT_PLACE_SYSTEM_TEXT = (
        "The user's sentence indicates a PLACE action. "
        "Your task is ONLY to extract WHERE the object should be dropped into or onto. "
        "Do not extract the object being moved. Extract only the destination phrase. "
        "Output JSON only with exactly two fields: {\"targetText\":\"<TEXT>\",\"targetType\":\"<TYPE>\"}. "
        "targetText must be the destination words from the user sentence, including color and descriptors when present. "
        "targetType must be exactly one of: cube, bowl, hand, misc. "
        "Use bowl for destination containers/surfaces like bowl, plate, cup, container, dish, bin, tray. "
        "Use hand only when destination is a person handoff (me/my hand/hand it to me/give it to me/pass it to me). "
        "Use cube only if the destination itself is another cube/block/box object. "
        "Use misc only if none of cube, bowl, or hand apply to the destination phrase. "
        "Critical rule: when both source object and destination appear, return the destination only. "
        "Example: 'put the yellow cube in the green bowl' -> {\"targetText\":\"green bowl\",\"targetType\":\"bowl\"}. "
        "Example: 'drop the red block onto the blue box' -> {\"targetText\":\"blue box\",\"targetType\":\"cube\"}. "
        "Example: 'place it in the tray' -> {\"targetText\":\"tray\",\"targetType\":\"bowl\"}. "
        "Example: 'give it to me' -> {\"targetText\":\"me\",\"targetType\":\"hand\"}. "
        "Example: 'hand that to my colleague' -> {\"targetText\":\"my colleague\",\"targetType\":\"hand\"}."
    )

    def invokeLLM(systemText, maxNewTokens=500):
        """Send one chat prompt to the LLM and return its decoded text response."""
        tokenizer = LLM_TOKENIZER
        model = LLM_MODEL_INSTANCE
        messages = [
            {"role": "system", "content": systemText},
            {"role": "user", "content": f"User command: {userText}"},
        ]

        prompt = tokenizer.apply_chat_template(
            messages,
            tokenize = False,
            add_generation_prompt = True,
            enable_thinking = False, # Suppress internal "thinking" steps from appearing in the output
        )
        inputs = tokenizer([prompt], return_tensors="pt").to(model.device)

        with torch.no_grad():
            outputIds = model.generate(
                **inputs,
                max_new_tokens=maxNewTokens
            )

        inputLength = inputs["input_ids"].shape[-1]
        newTokenIds = outputIds[0][inputLength:]
        return tokenizer.decode(newTokenIds, skip_special_tokens=True).strip()

    def extractJson(rawResponse):
        """Extract and parse the first JSON object span from LLM text output, trimming any surrounding fluff before and after."""
        start = rawResponse.find("{")
        end = rawResponse.rfind("}")
        if start == -1 or end == -1 or end < start:
            return {}
        try:
            parsedResponse = json.loads(rawResponse[start:end + 1])
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsedResponse, dict):
            return {}

        return parsedResponse

    # Note: possible toolCalls are 'pick', 'place', 'pickAndPlace', 'stop', 'none'
    toolCall = extractJson(invokeLLM(EXTRACT_TOOL_CALL_SYSTEM_TEXT, maxNewTokens=20)).get("tool", "none")

    if toolCall == "none":
        return {
            "toolCall": "none"
        }

    if toolCall == "stop":
        return {
            "toolCall": "stop"
        }

    if toolCall == "pick":
        return {
            "toolCall": "pick",
            "pickTarget": extractJson(invokeLLM(EXTRACT_PICK_SYSTEM_TEXT)),
        }

    if toolCall == "place":
        return {
            "toolCall": "place",
            "placeTarget": extractJson(invokeLLM(EXTRACT_PLACE_SYSTEM_TEXT)),
        }

    if toolCall == "pickAndPlace":
        return {
            "toolCall": "pickAndPlace",
            "pickTarget": extractJson(invokeLLM(EXTRACT_PICK_SYSTEM_TEXT)),
            "placeTarget": extractJson(invokeLLM(EXTRACT_PLACE_SYSTEM_TEXT)),
        }

    return {
        "toolCall": "none"
    }

############################
### ROBOT MOTION CONTROL ###
############################

def robotPlanMovementQueue(toolCallJSON):
    """Convert parsed tool call into a fresh ordered queue of robot movements"""
    global robotMovementQueue
    global robotCurrentMovement

    toolCall = toolCallJSON.get("toolCall", "none")

    # For 'none', keep current execution state untouched
    if toolCall == "none":
        return

    # For actionable calls, reset current movement and rebuild queue
    robotCurrentMovement = None
    robotMovementQueue = []

    if toolCall == "stop":
        robotMovementQueue.append({"movement": "stop"})
        return

    if toolCall == "pick":
        pickTarget = toolCallJSON.get("pickTarget")
        robotMovementQueue.extend(
            [
                {"movement": "moveGripperToValue", "value": 100}, # Ensure gripper is open
                {"movement": "moveToReadyPosition"}, # Lean back to maximize camera view of the work area
                {"movement": "movetoAboveTarget", "target": pickTarget}, # Move above the pick target
                {"movement": "moveToHeight", "height": 105}, # Descend to pick height
                {"movement": "moveGripperToValue", "value": 40}, # Grasp object
                {"movement": "moveToHeight", "height": 225}, # Ascend back to safe height
            ]
        )
        return

    if toolCall == "place":
        placeTarget = toolCallJSON.get("placeTarget")
        robotMovementQueue.extend(
            [
                {"movement": "movetoAboveTarget", "target": placeTarget}, # Move above the place target
                {"movement": "moveToHeight", "height": 165}, # Descend about halfway
                {"movement": "moveGripperToValue", "value": 100}, # Release object
                {"movement": "moveToReadyPosition"}, # Get ready to start over
            ]
        )
        return

    if toolCall == "pickAndPlace":
        pickTarget = toolCallJSON.get("pickTarget")
        placeTarget = toolCallJSON.get("placeTarget")
        robotMovementQueue.extend(
            [
                {"movement": "moveGripperToValue", "value": 100}, # Ensure gripper is open
                {"movement": "moveToReadyPosition"}, # Lean back to maximize camera view of the work area
                {"movement": "movetoAboveTarget", "target": pickTarget}, # Move above the pick target
                {"movement": "moveToHeight", "height": 105}, # Descend to pick height
                {"movement": "moveGripperToValue", "value": 40}, # Grasp object
                {"movement": "moveToHeight", "height": 225}, # Ascend back to safe height
                {"movement": "movetoAboveTarget", "target": placeTarget}, # Move above the place target
                {"movement": "moveToHeight", "height": 165}, # Descend about halfway
                {"movement": "moveGripperToValue", "value": 100}, # Release object
                {"movement": "moveToReadyPosition"}, # Get ready to start over
            ]
        )
        return

def robotControllerTick():
    """Proceed one tick of robot-control cycle: either do current movement, start next movement, or just wait to avoid overwhelming robot firmware"""
    global robotMovementQueue
    global robotCurrentMovement
    global robotLastCommandSentTime
    now = time.time()

    # Compute error magnitude between coordinates for completion check
    def positionDifferenceMm(coordsA, coordsB):
        if coordsA is None or coordsB is None:
            return None
        ax, ay, az = coordsA[0], coordsA[1], coordsA[2]
        bx, by, bz = coordsB[0], coordsB[1], coordsB[2]
        return ((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2) ** 0.5

    # Compute error magnitude between angles for completion check
    def angleDifferenceDeg(anglesA, anglesB):
        if anglesA is None or anglesB is None:
            return None
        return max(abs(anglesA[i] - anglesB[i]) for i in range(len(anglesB)))

    # Limit commands to robot to prevent overwhelming its firmware
    if robotLastCommandSentTime is not None and (now - robotLastCommandSentTime) < 0.1:
        return

    # Pop the next movement
    if robotCurrentMovement is None:
        if not robotMovementQueue:
            return
        robotCurrentMovement = robotMovementQueue.pop(0)
        minute = int((now // 60) % 60)
        second = now % 60
        RICH.print(f"[bold {PURPLE}]MOVEMENT:[/bold {PURPLE}] [{BLUE}]{robotCurrentMovement}[/{BLUE}]")

    movementType = robotCurrentMovement.get("movement")

    # Stop immediately and clear queued work
    if movementType == "stop":
        ROBOT.stop()
        robotLastCommandSentTime = now
        robotMovementQueue = []
        robotCurrentMovement = None
        return

    # Drive to ready pose and wait until within joint-angle tolerance
    if movementType == "moveToReadyPosition":
        readyAngles = [-30, 45, -45, 0, 30, -45]
        currentAngles = ROBOT.get_angles()
        angleDiff = angleDifferenceDeg(currentAngles, readyAngles)
        if angleDiff is not None and angleDiff <= 2:
            robotCurrentMovement = None
            return
        if ROBOT.is_moving() == 1:
            return
        ROBOT.send_angles(readyAngles, ROBOT_SPEED)
        robotLastCommandSentTime = now
        return

    if movementType == "movetoAboveTarget":
        target = robotCurrentMovement.get("target")
        targetCoords = robotCurrentMovement.get("targetCoords")
        if targetCoords is None:
            targetCoords = getTargetCoordsFromVision(target)
            if targetCoords is None:
                return
            robotCurrentMovement["targetCoords"] = targetCoords

        targetX, targetY, _, targetRz = targetCoords
        desiredCoords = [targetX, targetY, 200, targetRz]

        # Phase 1: Send move command to get above target
        if not robotCurrentMovement.get("aboveTargetCommandSent", False):
            ROBOT.send_coords([targetX, targetY, 200, 180, 0, targetRz], ROBOT_SPEED)
            robotCurrentMovement["aboveTargetCommandSent"] = True
            robotLastCommandSentTime = now
            return

        # Phase 2: Wait until motion ends
        if ROBOT.is_moving() == 1:
            return

        # Verify that robot is actually near desired position before considering the move complete
        currentCoords = ROBOT.get_coords()
        if isinstance(currentCoords, (list, tuple)) and len(currentCoords) >= 3:
            currentXyz = [currentCoords[0], currentCoords[1], currentCoords[2], 0]
            posErrorMm = positionDifferenceMm(currentXyz, desiredCoords)
            if posErrorMm is not None and posErrorMm > 20:
                ROBOT.send_coords([targetX, targetY, 200, 180, 0, targetRz], ROBOT_SPEED)
                robotLastCommandSentTime = now
                return

        # Phase 3: Confirm that target hasn't moved
        refreshedTargetCoords = getTargetCoordsFromVision(target)
        if refreshedTargetCoords is not None:
            driftMm = positionDifferenceMm(targetCoords, refreshedTargetCoords)
            if driftMm is not None and driftMm > 2:
                robotCurrentMovement["targetCoords"] = refreshedTargetCoords
                robotCurrentMovement["aboveTargetCommandSent"] = False
                return

        # Stable enough: proceed to next movement
        robotCurrentMovement = None
        return

    if movementType == "moveGripperToValue":
        targetValue = robotCurrentMovement.get("value", 100)
        gripperValue = ROBOT.get_gripper_value()
        if gripperValue is not None and abs(gripperValue - targetValue) <= 15:
            robotCurrentMovement = None
            return
        ROBOT.set_gripper_value(targetValue, ROBOT_SPEED)
        robotLastCommandSentTime = now
        return

    if movementType == "moveToHeight":
        currentCoords = ROBOT.get_coords()
        if not (isinstance(currentCoords, (list, tuple)) and len(currentCoords) >= 6):
            return

        movement = robotCurrentMovement

        # Build a one-axis Cartesian path so all joints move together
        if "path" not in movement:
            startX, startY, startZ, fixedRx, fixedRy, fixedRz = currentCoords[:6]
            deltaZ = movement["height"] - startZ
            if abs(deltaZ) <= 2:
                robotCurrentMovement = None
                return

            stepCount = max(1, int(np.ceil(abs(deltaZ) / 10.0))) # divide the total Z distance into 10mm increments
            movement["path"] = (startX, startY, startZ, deltaZ, fixedRx, fixedRy, fixedRz, stepCount, 0)

        startX, startY, startZ, deltaZ, fixedRx, fixedRy, fixedRz, stepCount, stepIndex = movement["path"]
        if stepIndex >= stepCount:
            robotCurrentMovement = None
            return

        stepIndex += 1
        nextZ = startZ + deltaZ * (stepIndex / stepCount)
        ROBOT.send_coords([startX, startY, nextZ, fixedRx, fixedRy, fixedRz], 100)
        movement["path"] = (startX, startY, startZ, deltaZ, fixedRx, fixedRy, fixedRz, stepCount, stepIndex)
        robotLastCommandSentTime = now
        return

######################
### INITIALIZATION ###
######################

# GPU
GPU = None

# Microphone
MICROPHONE_INDEX = None
MICROPHONE_SAMPLERATE = None
MICROPHONE_BLOCK_SIZE = None # aka "chunk" size for audio processing

# Camera
CAMERA = None

# Robot
ROBOT = None
ROBOT_SPEED = 50
robotMovementQueue = []
robotCurrentMovement = None
robotLastCommandSentTime = None

# Speech model
SPEECH_MODEL_ID = "nvidia/parakeet-tdt-0.6b-v2"
SPEECH_MODEL = None

# Language model
LLM_MODEL_ID = "Qwen/Qwen2.5-1.5B-Instruct"
LLM_TOKENIZER = None
LLM_MODEL_INSTANCE = None

# Vision models
HAND_MODEL = os.path.join(os.path.dirname(__file__), "hand_landmarker.task")
HAND_LANDMARKER = None
OWL_VIT_MODEL_ID = "google/owlvit-base-patch32"
OWL_VIT_PROCESSOR = None
OWL_VIT_MODEL_INSTANCE = None

# Runtime state
newestUtterance = None
newestToolCall = None

with printVerbosely("Check for GPU"):
    hasGPU = torch.cuda.is_available()
    if hasGPU:
        GPU = torch.device("cuda:0")
    else:
        raise RuntimeError("CUDA is not available")

with printVerbosely("Prepare microphone"):
    allMicrophones = listMicrophones()
    if not allMicrophones:
        raise RuntimeError("No microphone found")
    bestMicrophone = selectBestMicrophone(allMicrophones)
    MICROPHONE_INDEX = int(bestMicrophone["Index"])
    MICROPHONE_SAMPLERATE = int(bestMicrophone["SampleRate"])
    MICROPHONE_BLOCK_SIZE = int(MICROPHONE_SAMPLERATE / 10) # Blocks of 1/10th of a second of audio

with printVerbosely("Prepare camera"):
    allCameras = listCameras()
    if not allCameras:
        raise RuntimeError("No camera found")
    cameraIndex = int(selectBestCamera(allCameras)["Index"])
    CAMERA = cv2.VideoCapture(cameraIndex, cv2.CAP_V4L2)
    if not CAMERA.isOpened():
        raise RuntimeError(f"Could not open camera {cameraIndex}")
    # Prevent camera from buffering many frames ahead
    CAMERA.set(cv2.CAP_PROP_BUFFERSIZE, 1)

with printVerbosely("Prepare robot"):
    from pymycobot.mycobot280 import MyCobot280
    robotPort = "/dev/ttyACM0"
    robotBaud = 115200
    ROBOT = MyCobot280(robotPort, robotBaud)

with printVerbosely(f"Load speech model: {SPEECH_MODEL_ID}"):
    with suppressStdOut():
        import nemo.collections.asr as nemo_asr
        SPEECH_MODEL = nemo_asr.models.ASRModel.from_pretrained(SPEECH_MODEL_ID)
        SPEECH_MODEL = SPEECH_MODEL.to("cpu") # Do NOT use GPU

with printVerbosely(f"Load language model: {LLM_MODEL_ID}"):
    from transformers import AutoTokenizer, AutoModelForCausalLM
    LLM_TOKENIZER = AutoTokenizer.from_pretrained(LLM_MODEL_ID)
    torchDtype = torch.float16 if torch.cuda.is_available() else torch.float32
    LLM_MODEL_INSTANCE = AutoModelForCausalLM.from_pretrained(
        LLM_MODEL_ID,
        dtype=torchDtype,
        device_map="auto", # Automatically use GPU if available
    )

with printVerbosely(f"Load vision model: {HAND_MODEL}"):
    with suppressStdOut():
        import mediapipe as mp
        from mediapipe.tasks.python import vision as _mpVision
        from mediapipe.tasks import python as _mpPython
        baseOptions = _mpPython.BaseOptions(model_asset_path=HAND_MODEL)
        options = _mpVision.HandLandmarkerOptions(
            base_options=baseOptions,
            num_hands=1,
            min_hand_detection_confidence=0.6,
            min_tracking_confidence=0.5,
            running_mode=_mpVision.RunningMode.IMAGE, # CPU
        )
        HAND_LANDMARKER = _mpVision.HandLandmarker.create_from_options(options)

with printVerbosely(f"Load vision model: {OWL_VIT_MODEL_ID}"):
    from transformers import OwlViTProcessor, OwlViTForObjectDetection
    OWL_VIT_PROCESSOR = OwlViTProcessor.from_pretrained(OWL_VIT_MODEL_ID)
    OWL_VIT_MODEL_INSTANCE = OwlViTForObjectDetection.from_pretrained(OWL_VIT_MODEL_ID)
    OWL_VIT_MODEL_INSTANCE = OWL_VIT_MODEL_INSTANCE.to("cpu") # CPU
    OWL_VIT_MODEL_INSTANCE.eval()

#################
### MAIN LOOP ###
#################

if __name__ == "__main__":
    speechCallback = makeSpeechCallback(inputSampleRate=MICROPHONE_SAMPLERATE)
    speechStopEvent, speechWorkerThread = startSpeechPipelineWorker()

    with sd.InputStream(device=MICROPHONE_INDEX, channels=1, samplerate=MICROPHONE_SAMPLERATE, blocksize=MICROPHONE_BLOCK_SIZE, callback=speechCallback):
        print("Listening... Press Ctrl+C to stop.")
        try:
            while True:
                # If the speech pipeline has produced a new tool call, re-plan immediately
                currentToolCall = newestToolCall
                newestToolCall = None
                if currentToolCall is not None:
                    toolCallJSON = currentToolCall["toolCallJSON"]
                    RICH.print(f"[bold {PURPLE}]TOOL CALL:[/bold {PURPLE}]")
                    RICH.print_json(json=json.dumps(toolCallJSON))
                    robotPlanMovementQueue(toolCallJSON)
                # Regardless, keep on executing!
                robotControllerTick()
                sd.sleep(50)

        except KeyboardInterrupt:
            print("Stopping...")

        finally:
            speechStopEvent.set()
            speechWorkerThread.join(timeout=1.0)
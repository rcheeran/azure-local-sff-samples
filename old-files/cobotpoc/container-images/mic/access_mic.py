import os
import sys
import glob
import time
import socket
import numpy as np
import scipy as sp
import sounddevice as sd
import soundfile as sf
import queue
from contextlib import contextmanager


def runStartupPreflight():
    """Print startup diagnostics for audio device visibility and model download connectivity."""

    print("=== Startup preflight ===")

    # Audio device visibility from container namespace
    sndPath = "/dev/snd"
    if os.path.isdir(sndPath):
        sndNodes = sorted(glob.glob(f"{sndPath}/*"))
        print(f"ALSA path found: {sndPath} ({len(sndNodes)} nodes)")
        if sndNodes:
            print("ALSA nodes:", ", ".join(os.path.basename(node) for node in sndNodes))
    else:
        print("WARNING: /dev/snd not found. Mount audio devices with: -v /dev/snd:/dev/snd")

    # Enumerate input devices through PortAudio/sounddevice
    try:
        devices = sd.query_devices()
        inputDevices = [d for d in devices if d.get("max_input_channels", 0) > 0]
        print(f"PortAudio inputs detected: {len(inputDevices)}")
        for idx, device in enumerate(inputDevices[:5]):
            print(
                f"  Input {idx + 1}: {device.get('name')} "
                f"(channels={device.get('max_input_channels')}, sample_rate={device.get('default_samplerate')})"
            )
    except Exception as err:
        print(f"WARNING: PortAudio query failed: {err}")

    # Validate model download prerequisites
    try:
        socket.getaddrinfo("huggingface.co", 443, type=socket.SOCK_STREAM)
        print("DNS check: huggingface.co resolved")
    except Exception as err:
        print(f"WARNING: DNS check failed for huggingface.co: {err}")
        print("Hint: try --network host if Docker bridge DNS cannot resolve external names")

    try:
        import requests

        response = requests.get("https://huggingface.co", timeout=10)
        print(f"HTTPS check: huggingface.co reachable (status={response.status_code})")
    except Exception as err:
        print(f"WARNING: HTTPS check failed for huggingface.co: {err}")

    print("=== Preflight complete ===")

@contextmanager
def printVerbosely(codeBlockDescription):
    """Print to console before/after block, report outcome and duration"""

    # Print before message
    print(f"{codeBlockDescription}...")
    start = time.time()
    success = True
    
    try:
        yield
    except Exception:
        success = False
        raise

    # Print after message in green or red

    finally:
        elapsed = time.time() - start
        COLORS = {
            "G" : "\033[92m", # Green
            "R" : "\033[91m", # Red
            "W" : "\033[0m", # Default, usually white
        }
        if success:
            print(f"{COLORS['G']}✔ {codeBlockDescription} succeeded in {elapsed:.2f}s{COLORS['W']}")
        else:
            print(f"{COLORS['R']}✗ {codeBlockDescription} failed after {elapsed:.2f}s{COLORS['W']}")

@contextmanager
def suppressStdOut():
    """Suppress stdout and stderr at the OS file descriptor level (catches C++ output from OpenCV, NeMo, etc)"""
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

def listMicrophones():
    """Return available microphones as dictionaries of index, name, channels, default_samplerate"""
    devices = sd.query_devices()

    if not devices:
        return None

    allMicrophones = []
    for index, device in enumerate(devices):

        # Skip devices with no input channels
        if device["max_input_channels"] <= 0:
            continue

        # Skip virtual/system devices with suspiciously high channel counts
        if device["max_input_channels"] > 8:
            continue

        # Skip devices with no default sample rate
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
    """
    Given list of microphones, select
        1. The first that includes "USB" in the name, else
        2. The first that has exactly 1 input audio channel, else
        3. The first overall.
    """

    if not allMicrophones:
        return None

    # 1. Prefer single-channel microphones
    monoMics = [mic for mic in allMicrophones if mic["Channels"] == 1]
    if monoMics:
        return monoMics[0]
    
    # 2 Prefer USB microphones
    usbMics = [mic for mic in allMicrophones if "usb" in mic["Name"].lower()]
    if usbMics:
        return usbMics[0]

    # 3. Fallback: return the first device
    return allMicrophones[0]

def initialize():
    global  MICROPHONE_INDEX, MICROPHONE_SAMPLERATE, SPEECH_MODEL

    # MICROPHONE
    with printVerbosely("Prepare microphone"):
        allMicrophones = listMicrophones()
        if not allMicrophones:
            raise RuntimeError("No microphone found")
        bestMicrophone = selectBestMicrophone(allMicrophones)
        print(f"Selected microphone: {bestMicrophone['Name']} (Index: {bestMicrophone['Index']}, Channels: {bestMicrophone['Channels']}, SampleRate: {bestMicrophone['SampleRate']})")
        MICROPHONE_INDEX = int(bestMicrophone["Index"])
        MICROPHONE_SAMPLERATE = int(bestMicrophone["SampleRate"])

    # SPEECH MODEL
    with printVerbosely("Load speech model: parakeet-tdt-0.6b-v2"):
        #with suppressStdOut():
            import nemo.collections.asr as nemo_asr
            SPEECH_MODEL = nemo_asr.models.ASRModel.from_pretrained("nvidia/parakeet-tdt-0.6b-v2")
            SPEECH_MODEL = SPEECH_MODEL.to("cpu")

def makeSpeechCallback(utteranceQueue, minTalkingThreshold=10, maxSilenceThreshold=10, quietThreshold=0.01):
    audioBuffer = []
    silenceCount = 0
    talkingCount = 0

    def callback(indata, frames, callback_time, status):
        """Callback that buffers speech, segments utterances, and pushes segmented audio to a queue."""
        nonlocal audioBuffer, silenceCount, talkingCount

        def isTooQuiet(chunk, threshold=0.03):
            rms = np.sqrt(np.mean(chunk ** 2))
            return rms < threshold

        # Extract, downsample, add to buffer
        audioChunk = indata[:, 0].astype(np.float32)
        audioChunkDownsampled = sp.signal.resample_poly(audioChunk, up=1, down=3)
        audioBuffer.append(audioChunkDownsampled)

        # Use energy threshold to track speaking vs silence in callback thread.
        if isTooQuiet(audioChunkDownsampled, threshold=quietThreshold):
            silenceCount += 1
        else:
            talkingCount += 1
            silenceCount = 0

        # If there hasn't been maxSilenceThreshold of consecutive silence yet, keep listening
        if silenceCount < maxSilenceThreshold:
            return

        # If we've hit maxSilenceThreshold, was there also enough talking? If yes, queue full utterance audio
        if talkingCount >= minTalkingThreshold and len(audioBuffer) >= (minTalkingThreshold + maxSilenceThreshold):
            fullAudio = np.concatenate(audioBuffer, axis=0)
            utteranceQueue.put(fullAudio)

        # Whether or not there was talking, start over
        audioBuffer = []
        silenceCount = 0
        talkingCount = 0

    return callback

def transcribeSpeechCommand(speechModel, audio):
    """Transcribe one utterance audio buffer into text."""
    return speechModel.transcribe([audio], verbose=False)[0].text.strip()


def transitionToListeningState():
    global state, query, lookUntil

    state = STATE_LISTENING
    print("Right now I am ... "+state)
    lookUntil = 0.0

    # Clean up
    query = None
    

def transitionToLookingState():
    global state, query, lookUntil

    state = STATE_LOOKING
    print(f"Right now I am ... "+state)
    lookUntil = time.time() + 10.0

################## MAIN CODE ##################

GPU = None


# Speech

MICROPHONE_INDEX = None
MICROPHONE_SAMPLERATE = None
SPEECH_MODEL = None

runStartupPreflight()
initialize() # Assigns all the global variables above

utteranceQueue = queue.Queue()
SPEECH_CALLBACK = makeSpeechCallback(utteranceQueue)

# State

STATE_LISTENING = "LISTENING"
STATE_LOOKING = "LOOKING"

state = STATE_LISTENING
query = None
lookUntil = 0.0

transitionToListeningState()

# Open microphone stream
with sd.InputStream(device=MICROPHONE_INDEX, channels=1, samplerate=MICROPHONE_SAMPLERATE, blocksize=int(MICROPHONE_SAMPLERATE / 10), dtype="float32", callback=SPEECH_CALLBACK):
    print("Listening... Press Ctrl+C to stop.")
    try:
        while True:
            now = time.time()
            if state == STATE_LISTENING:
                try:
                    # If we have a new utterance, transcribe and switch to looking
                    utteranceAudio = utteranceQueue.get_nowait()
                    print("I have an utterance: ", utteranceAudio)
                    fullText = transcribeSpeechCommand(SPEECH_MODEL, utteranceAudio)
                    if fullText:
                        print(f"Heard: '{fullText}'")
                        #transitionToLookingState()
                except queue.Empty:
                    pass

            elif state == STATE_LOOKING:
                # If it's been 10 seconds, go back to listening
                if now >= lookUntil:
                    transitionToListeningState()
                # If we have a query, use the camera to look for it
                sd.sleep(50)
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        print("Entered Finally and now Stopping...")
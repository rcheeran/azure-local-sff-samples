# =========================================================================== #
# vision-service                                                               #
#                                                                              #
# FastAPI HTTP wrapper around the four detectors from cobotpoc.py:             #
#   - lookForCube  (HSV + contour + minAreaRect)                               #
#   - lookForBowl  (HSV + contour + circularity)                               #
#   - lookForHand  (MediaPipe HandLandmarker)                                  #
#   - lookForMisc  (HuggingFace OWL-ViT, open-vocabulary)                      #
#                                                                              #
# CPU-only on purpose: avoids the host's nvidia-container-toolkit 1.17 vs.     #
# CDI-hook bug that crashes the Qwen-GPU and Parakeet-GPU pods on this         #
# cluster.                                                                     #
#                                                                              #
# Build (run on this host where docker is installed):                          #
#   cd /home/clouduser/demo/cobotpoc/container-images/vision                   #
#   cp ../../hand_landmarker.task ./hand_landmarker.task                       #
#   sudo docker build --network=host -t vision-service:local .                 #
#                                                                              #
# Import into k3s containerd:                                                  #
#   sudo docker save vision-service:local \                                    #
#       | sudo /usr/local/bin/k3s ctr -n k8s.io images import -                #
#                                                                              #
# Verify:                                                                      #
#   sudo /usr/local/bin/k3s ctr -n k8s.io images ls | grep vision-service      #
# =========================================================================== #
FROM python:3.11-slim

# OpenCV (headless) needs libgl/libglib at runtime; ffmpeg is harmless and
# lets us decode video chunks if a future endpoint accepts them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         libgl1 libglib2.0-0 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps. We force the CPU-only torch wheel to keep the image
# under 2 GB (the default CUDA wheel is ~3 GB on its own).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r /tmp/requirements.txt

WORKDIR /app

# MediaPipe model. Downloaded out-of-band from
# https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task
# and copied into the build context (see README).
COPY hand_landmarker.task /app/models/hand_landmarker.task

COPY app /app/app

# HuggingFace cache lives on a mounted PVC so OWL-ViT (~580 MB) isn't pulled
# every time the pod restarts.
ENV HF_HOME=/cache/huggingface
ENV TRANSFORMERS_CACHE=/cache/huggingface

# Service config. All overridable via Deployment env.
ENV VISION_PORT=8000
ENV VISION_HAND_MODEL=/app/models/hand_landmarker.task
ENV VISION_OWLVIT_MODEL_ID=google/owlvit-base-patch32
ENV VISION_DEVICE=cpu

EXPOSE 8000
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

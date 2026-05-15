# =========================================================================== #
# Parakeet ASR server image                                                    #
#                                                                              #
# Base: nvcr.io/nvidia/pytorch:24.07-py3                                       #
#   - Pre-installed CUDA 12.5 + PyTorch 2.4 + cuDNN, suitable for Parakeet's   #
#     RNNT decoder kernels and HF wheel compatibility.                         #
#   - Already common in NGC-based clusters; if you don't have it cached, the   #
#     first build pulls ~9 GB.                                                 #
#                                                                              #
# Build (run on this host where docker is installed):                          #
#   cd /home/clouduser/demo/parakeet                                           #
#   docker build -t parakeet-server:local .                                    #
#                                                                              #
# Import into k3s containerd (skips needing a registry):                       #
#   docker save parakeet-server:local \                                        #
#       | sudo /usr/local/bin/k3s ctr -n k8s.io images import -                #
#                                                                              #
# Verify it landed in the cluster's image store:                               #
#   sudo /usr/local/bin/k3s ctr -n k8s.io images ls | grep parakeet-server     #
#                                                                              #
# Then `kubectl apply -f foundry_yamls/parakeet_gpu.yaml`. The Deployment uses #
# `imagePullPolicy: Never`, so no registry push is needed.                     #
# =========================================================================== #
FROM nvcr.io/nvidia/pytorch:24.07-py3

# System deps for audio decoding (libsndfile via soundfile, ffmpeg for mp3/m4a).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# NeMo ASR + the FastAPI runtime. Pin nemo_toolkit to a recent stable line; the
# container's PyTorch is 2.4 so any nemo_toolkit >= 2.0 works.
# `python-multipart` is required by FastAPI for `UploadFile`/`Form`.
RUN pip install --no-cache-dir \
        "nemo_toolkit[asr]==2.0.0" \
        "fastapi==0.115.4" \
        "uvicorn[standard]==0.32.0" \
        "python-multipart==0.0.20" \
        "soundfile==0.12.1" \
        "scipy>=1.10"

WORKDIR /app
COPY server.py /app/server.py

# Send HuggingFace cache to a known location so a PVC mount can persist it.
ENV HF_HOME=/cache/huggingface
ENV NEMO_CACHE_DIR=/cache/nemo
ENV PARAKEET_MODEL=nvidia/parakeet-tdt-0.6b-v2
ENV PARAKEET_DEVICE=cuda
ENV PARAKEET_PORT=8000

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

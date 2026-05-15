FROM ubuntu:24.04

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install Python, audio runtime/dev libraries, and compiler toolchain.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    build-essential \
    pkg-config \
    ca-certificates \
    git \
    curl \
    libgomp1 \
    portaudio19-dev \
    libportaudio2 \
    alsa-utils \
    alsa-ucm-conf \
    alsa-topology-conf \
    libasound2t64 \
    libasound2-dev \
    libsndfile1 \
    libsndfile1-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt

# Copy the application
COPY access_mic.py .

# Run the application
CMD ["python3", "access_mic.py"]

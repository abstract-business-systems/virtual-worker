# abs-workspace — the isolated virtual worker environment.
# Runs as Linux user `abs-worker` inside the container; reachable via
#   ssh abs-worker@localhost   (host port 2222)
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip \
        xvfb pulseaudio pulseaudio-utils \
        tesseract-ocr tesseract-ocr-eng \
        espeak-ng alsa-utils ffmpeg \
        openssh-server sudo ca-certificates curl \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd \
    && useradd --create-home --shell /bin/bash abs-worker \
    && echo "abs-worker:abs-worker" | chpasswd

# ---- python deps in a venv -----------------------------------------------------
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir \
        "faster-whisper==1.0.3" "playwright==1.45.0" "mss==9.0.1" \
        "pillow==10.3.0" "pytesseract==0.3.13" numpy
ENV PATH="/opt/venv/bin:${PATH}"
RUN playwright install chromium 2>/dev/null || playwright install --with-deps chromium

# ---- whisper model baked in so first question is fast --------------------------
RUN python3 -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8')" \
    || PW_MODEL=base.en python3 -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-base.en')" \
    || true

# ---- code is mounted at /opt/virtual-worker (see docker-compose.yml) -----------
RUN mkdir -p /opt/virtual-worker /run/virtual-worker /var/lib/virtual-worker \
    && chown -R abs-worker:abs-worker /opt/virtual-worker /run/virtual-worker \
                                     /var/lib/virtual-worker /home/abs-worker \
    && ln -sf /opt/virtual-worker/deploy/vw-wrapper.sh /usr/local/bin/vw \
    && ln -sf /opt/virtual-worker/deploy/vw-wrapper.sh /usr/local/bin/vw-admin \
    && chmod +x /opt/virtual-worker/deploy/vw-wrapper.sh 2>/dev/null || true

# ensure the user's shell can run vw and the venv python
RUN echo 'export PATH="/opt/venv/bin:/usr/local/bin:$PATH"' >> /home/abs-worker/.bashrc \
    && echo 'export DISPLAY=:99' >> /home/abs-worker/.bashrc \
    && echo 'export PYTHONPATH="/opt/virtual-worker/common:/opt/virtual-worker/backend:/opt/virtual-worker/agent"' >> /home/abs-worker/.bashrc

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 22
ENTRYPOINT ["/entrypoint.sh"]
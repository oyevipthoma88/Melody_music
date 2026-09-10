# ════════════════════════════════════════════════════════════
#   𝙈𝙚𝙡𝙤𝙙𝙞𝙓 🎧  —  Docker Image
#   Owner  : @oyevipthoma88
#   Bot    : @YourMelodyBot
# ════════════════════════════════════════════════════════════

FROM nikolaik/python-nodejs:python3.10-nodejs18

# Install system dependencies
RUN apt-get update -y && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        wget \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
COPY . /app/
WORKDIR /app/

# Install Python dependencies
RUN pip3 install --no-cache-dir --upgrade pip \
    && pip3 install --no-cache-dir --upgrade --requirement requirements.txt

# heroku.yml deploys this repository through the Dockerfile, so Heroku's
# buildpack `bin/post_compile` hook is not invoked automatically. Without
# this explicit bootstrap the image ships without Deno/bgutil PO-token
# support and static ffmpeg, causing YouTube direct resolution and the local
# fallback downloader to fail together on cloud IPs.
RUN bash /app/bin/post_compile /app

# Start bot
CMD bash start

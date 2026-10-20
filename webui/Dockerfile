# Deep Dog 2 web UI on Hugging Face Spaces (Docker SDK).
# Built from the hf-spaces-docker-expert python-app template.
# Stage the Space repo with webui/build_space.sh; this file sits at its root
# next to app.py, requirements-ui.txt, pyproject.toml, deep_research/ and README.md.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# HF Spaces runs the container as UID 1000.
RUN if ! id -u 1000 >/dev/null 2>&1; then \
        useradd -m -u 1000 user; \
    else \
        if ! id -u user >/dev/null 2>&1; then \
            usermod -d /home/user -m -l user "$(id -un 1000)"; \
        fi; \
    fi

USER 1000
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    GRADIO_ANALYTICS_ENABLED=False \
    UI_MAX_MINUTES=30 \
    UI_MAX_CONCURRENT_RUNS=2
WORKDIR /home/user/app

# UI dependency first: it changes least, so this layer stays cached.
COPY --chown=1000:1000 requirements-ui.txt .
RUN pip install --user -r requirements-ui.txt

# The engine, installed as a package from the same tree.
COPY --chown=1000:1000 pyproject.toml README.md LICENSE ./
COPY --chown=1000:1000 deep_research ./deep_research
RUN pip install --user .

COPY --chown=1000:1000 app.py .

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7860/ || exit 1

# The app is the single process on the public port. No provider keys are baked in
# or required: visitors type their own. ACCESS_PASSWORD (a Space secret) adds a
# shared-password gate.
CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "7860"]

ARG BASE_IMAGE=resoluto-sandbox-base:dev
FROM ${BASE_IMAGE}

ARG IMAGE_VERSION
ENV RESOLUTO_IMAGE_VERSION=${IMAGE_VERSION}

USER root
RUN npm install -g @anthropic-ai/claude-code \
    && pip install --no-cache-dir --break-system-packages claude-agent-sdk
USER 1000

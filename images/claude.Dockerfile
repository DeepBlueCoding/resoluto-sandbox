# check=skip=InvalidDefaultArgInFrom
# BASE_IMAGE has NO default deliberately — images.py always passes it explicitly; a bare
# `docker build` without --build-arg BASE_IMAGE=... fails fast with a clear error instead of
# silently resolving a floating :dev/:latest tag.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG IMAGE_VERSION
ARG SDK_VERSION
LABEL resoluto.wheel_version=${IMAGE_VERSION}
ENV RESOLUTO_IMAGE_VERSION=${IMAGE_VERSION}

USER root
RUN npm install -g @anthropic-ai/claude-code \
    && pip install --no-cache-dir --break-system-packages claude-agent-sdk==${SDK_VERSION}
USER 1000

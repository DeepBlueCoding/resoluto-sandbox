ARG BASE_IMAGE=resoluto-sandbox-base:dev
FROM ${BASE_IMAGE}

ARG IMAGE_VERSION
ARG SDK_VERSION
LABEL resoluto.wheel_version=${IMAGE_VERSION}
ENV RESOLUTO_IMAGE_VERSION=${IMAGE_VERSION}

USER root
RUN pip install --no-cache-dir --break-system-packages openai-agents==${SDK_VERSION}
USER 1000

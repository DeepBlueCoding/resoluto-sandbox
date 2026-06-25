ARG BASE_IMAGE=resoluto-sandbox-base:dev
FROM ${BASE_IMAGE}

ARG IMAGE_VERSION
ENV RESOLUTO_IMAGE_VERSION=${IMAGE_VERSION}

USER root
RUN pip install --no-cache-dir --break-system-packages langchain langgraph langchain-anthropic
USER 1000

# check=skip=InvalidDefaultArgInFrom
# BASE_IMAGE has NO default deliberately — images.py always passes it explicitly; a bare
# `docker build` without --build-arg BASE_IMAGE=... fails fast with a clear error instead of
# silently resolving a floating :dev/:latest tag.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG IMAGE_VERSION
ARG SDK_VERSION
ARG LANGGRAPH_VERSION
LABEL resoluto.wheel_version=${IMAGE_VERSION}
ENV RESOLUTO_IMAGE_VERSION=${IMAGE_VERSION}

USER root
# Bare LangChain core + LangGraph only — NO provider integration baked in. LangChain itself is
# provider-agnostic; to actually call an LLM you need the matching integration package
# (langchain-anthropic, langchain-openai, langchain-google-genai, ...), which is NOT included here.
# Extend this Dockerfile with `RUN pip install langchain-<provider>==<version>` — see
# examples/langchain_agent.py and the "Extending the langchain image" recipe in docs/backends.md.
RUN pip install --no-cache-dir --break-system-packages langchain==${SDK_VERSION} langgraph==${LANGGRAPH_VERSION}
USER 1000

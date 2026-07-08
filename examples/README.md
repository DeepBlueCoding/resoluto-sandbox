# Examples

Two kinds of file live here — keep them straight:

- **Drivers** (`run_*.py`) — these USE the library (`from resoluto.sandbox import Sandbox`). They are
  what you read to learn resoluto-sandbox.
- **Payloads** (`payloads/`) — plain programs that run *inside* the sandbox. By the program contract
  they NEVER import `resoluto.sandbox`; a payload that works as `uv run payloads/x.py` on your host
  runs unchanged in the guest. They are the *cargo*, not the API.

## Start here

| Driver | Shows |
|--------|-------|
| `run_agent_in_sandbox.py <claude\|langchain\|openai\|openrouter>` | Run a provider's agent isolated in a Kata microVM — **symmetric across the provider images**. The provider you name selects the matching prebuilt image, payload, credential env, and egress host; nothing privileges one provider. `openrouter` is OpenAI-compatible: it reuses the `openai` image + payload, forwarding `OPENROUTER_API_KEY` as `OPENAI_API_KEY` with `OPENAI_BASE_URL=https://openrouter.ai/api/v1` (default model `mistralai/mistral-small-3.2-24b-instruct`; override with `OPENROUTER_MODEL`). |
| `run_hello_in_sandbox.py` | The bare mechanics — stage a plain program (`payloads/hello.py`) into the guest and run it on the base image. |

```bash
bash scripts/local-backend-up.sh                                # provision the local Kata backend → writes local.env (green canary)
set -a; source local.env; set +a                                # exports RESOLUTO_SANDBOX_IMAGE (base)
uv run python examples/run_hello_in_sandbox.py                   # simplest: hello, on the base image

resoluto-sandbox image build --provider openai                  # build+push the overlay to the registry (openrouter reuses it)
export OPENAI_API_KEY=...                                        # each provider brings its OWN credential
uv run python examples/run_agent_in_sandbox.py openai "why isolate agents?"
export ANTHROPIC_API_KEY=...
uv run python examples/run_agent_in_sandbox.py langchain "why isolate agents?"
export CLAUDE_CODE_OAUTH_TOKEN=$(claude setup-token)
uv run python examples/run_agent_in_sandbox.py claude "why isolate agents?"
export OPENROUTER_API_KEY=sk-or-...                              # OpenAI-compatible; reuses the openai image
uv run python examples/run_agent_in_sandbox.py openrouter "why isolate agents?"
```

`run_hello_in_sandbox.py` reads `RESOLUTO_SANDBOX_IMAGE` (the base image); `run_agent_in_sandbox.py`
resolves the provider overlay tag from the sandbox's own `image_tags()` and **registry-qualifies it**
(`images.pullable()` → `localhost:5000/resoluto-sandbox:…`). `image build` pushes the overlay to that
registry and the backend pulls it on demand, so **no manual `docker save | nerdctl load`** is needed —
build once, then run. (Why a registry at all? `docker build` and the `local` backend use two separate
image stores; the registry bridges them — see the repo README, "Prebuilt provider images".) Each run
forwards the provider's credential via `env=` — the sandbox never reads or parses a credential file.

## Payloads

`payloads/` holds one plain program per prebuilt provider image. Run any of them inside the sandbox by
staging `examples/payloads` as the workspace and giving the guest a path relative to it (e.g.
`argv=["python", "claude_agent.py", "..."]`, never `"payloads/claude_agent.py"`):

| Payload | Needs | Provider image |
|---------|-------|----------------|
| `hello.py` | nothing | any |
| `claude_agent.py` | Claude Max/Pro (`claude setup-token`) or `ANTHROPIC_API_KEY` | `resoluto-sandbox:claude-agent-sdk-<ver>` |
| `langchain_agent.py` | `ANTHROPIC_API_KEY` (+ `langchain-anthropic` added to the image) | `resoluto-sandbox:langchain-<ver>` |
| `openai_agent.py` | `OPENAI_API_KEY` | `resoluto-sandbox:openai-agents-<ver>` |

The end-to-end smoke tests that drive these payloads through BOTH backends (local + k8s) live in
`tests/smoke/` — they are test harnesses (GREEN/RED/BLOCKED, backend flags), not teaching examples.

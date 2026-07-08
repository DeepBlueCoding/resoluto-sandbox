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
| `run_agent_in_sandbox.py` | Run any untrusted program isolated in a Kata microVM — here a Claude agent as the sample workload — egress locked, input→output round-tripped through the store. |
| `run_hello_in_sandbox.py` | The bare mechanics — stage a plain program (`payloads/hello.py`) into the guest and run it under the local Kata backend. |

```bash
# provision the local Kata backend first (see the repo README), then:
set -a; source local.env; set +a                                       # exports RESOLUTO_SANDBOX_IMAGE
uv run python examples/run_hello_in_sandbox.py                         # simplest: hello, sandboxed
uv run python examples/run_agent_in_sandbox.py "why isolate agents?"   # a real agent, isolated
```

Neither driver hardcodes an image tag — both read `RESOLUTO_SANDBOX_IMAGE` (set when you provision the
backend) and fail fast if it is unset.

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

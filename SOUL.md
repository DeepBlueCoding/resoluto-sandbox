# SOUL.md — The Philosophy of resoluto-sandbox

This is the philosophical foundation of `resoluto-sandbox`. Read it before changing anything in
this repo. Come back to it when a design choice feels arbitrary — the answer is almost always here.

`resoluto-sandbox` is a **standalone, generic substrate**: it runs an arbitrary program in isolation
and lets that program exchange data with its caller through a durable store. It is published on its
own, has its own git history, and knows nothing about who uses it.

---

## The one-sentence thesis

**Run a program in isolation, and exchange its data through a durable store.**

Two verbs, deliberately paired: *isolate* and *rendezvous*. They are the two axes of this repo, and
the single most important idea here is that **they are orthogonal**. Everything else follows.

---

## The two axes are orthogonal (why the Conduit exists even though the sandbox is a microVM)

A recurring, reasonable question: *if the sandbox is a VM-grade microVM, why do we also need the
Conduit?* Because the microVM answers a different question than the Conduit does.

| Axis | Contract | Answers | Concrete impl |
|------|----------|---------|---------------|
| **Isolation** | `SandboxRuntime` | *Can adversarial code hurt the host?* | Kata microVM (`k8s`, `local`) |
| **Rendezvous** | `Conduit` | *How does state survive, and how does the caller learn what happened?* | `localfs`, `s3`, `gcs` |

Isolation and durability are independent — but the relationship is stronger than mere independence,
and this is the load-bearing insight of the repo: **strong isolation *demands* explicit rendezvous.**
A microVM deliberately severs every ambient channel processes normally share — shared memory, shared
filesystem, direct network. The more complete the isolation, the *more* you need one narrow,
explicit, well-defined channel to cross it. So the microVM does not make the Conduit redundant; the
microVM is precisely *why* the Conduit must exist in its clean, minimal form. The question "it's a
microVM, so do we still need the Conduit?" inverts the causality — the isolation is the reason the
rendezvous has to be explicit.

The Conduit earns its place three times over:

1. **State outlives the sandbox.** A run is a sequence of short-lived nodes — each node its own
   ephemeral sandbox. Between nodes **no sandbox is alive**. The checkpoint and workspace must live
   somewhere durable in the gap. That somewhere is the Conduit. This is what makes a **held run
   consume zero RAM** (there is no VM to hold) and what makes **resume** a plain `copy_prefix`
   forward.
2. **The sandbox is passive — no inbound port, no long-lived stream.** It self-reports append-only
   JSONL chunks to the Conduit; the caller launches, tails, and reaps. There is nothing to connect
   *to*. A long-lived streaming connection is the classic wedge (a reader blocks forever on a
   producer that quietly died); a store-mediated design is **structurally immune** to it.
3. **Crashes are durable.** Any crash writes a durable result to the store before the sandbox
   disappears. The caller reads a fact, never infers one from a dropped socket.

And the Conduit is the **backend-portability seam** — one interface, three transports, chosen by
where the sandbox runs. A single-host runtime shares a `localfs` Conduit over a bind mount; a
multi-node runtime *cannot* bind-mount a host path a sandbox may never land on, so it rendezvouses
through `s3`; the no-VM host substrate uses `localfs` directly. A microVM with a bind mount **is** a
Conduit — drop the abstraction and you re-implement it once per backend, you do not remove it. The
drive logic stays backend-agnostic only because the Conduit exists. This is the empirical answer to
"do we need it": the interface is what keeps every runtime interchangeable.

Delete the Conduit and you are forced into the opposite design: **one long-lived VM holding its own
state over a live connection.** That is strictly worse — held work costs RAM (so competitors "don't
fit"), and the long-lived stream reintroduces the wedge we deleted. The stepped-microVM + Conduit
design exists precisely to escape that trap. **Keep the Conduit.**

---

## The program stays plain

A script that runs with `uv run agent.py` on your machine runs **unchanged** inside the sandbox. The
program reads `argv`, writes `stdout` and files, and exits. It never imports `resoluto.sandbox`,
never learns it is sandboxed, never adopts an SDK. The verdict is derived **caller-side**; the
in-guest exit code is work product, not a control signal.

If a change would make programs import this package or learn about the substrate to run correctly,
the change is wrong.

---

## Zero coupling — the standing rule

This substrate runs *arbitrary* programs. **Nothing about any particular caller may live here** —
not in code, not in wording, not in env-var names, not in image names, not in config. The vocabulary
is deliberately generic: *program, run, node, host/caller, sandbox/runtime, store/Conduit*. If a
caller-specific concept (a domain workflow, an orchestrator's job, an agent, a pipeline) leaks into
this repo, it is a bug — excise it. A caller depends on the sandbox; the sandbox depends on no one.

---

## Isolation never degrades silently

VM-grade or nothing. The Kata runtime-class guard is **unconditional** — there is no trusted-fast
path, no "just this once" plain-container bypass. Egress is **fail-closed** by default (deny all but
what is explicitly allowed). A weaker posture is never selected as a fallback from a failed strong
one; a tier is only ever chosen **explicitly and loudly**. Security that can be silently downgraded
is not security.

---

## Liveness is silence, never a clock

There are no wall-clock timeouts anywhere — no `wait_for(timeout=)`, no `max_wall_seconds`, no
`timeout N`. **If work is alive, let it run.** Aliveness is proven by the heartbeat: only
substrate *silence* (no chunk within the window) reaps a sandbox, and the watchdog is armed only
once the sandbox is actually running. A sandbox that is pending, pulling, or scheduling is **waiting,
not dead** — it keeps waiting. Per-request socket bounds are fine (they bound one I/O call, not the
work); wall-clock deadlines on the work itself are forbidden.

---

## Fail fast, no fallbacks

The base install is pydantic-only; every heavy dependency is gated behind an extra
(`[s3]`, `[gcs]`, `[k8s]`) and imported lazily at its use site — this is enforced, not aspirational.
A broken plugin is a hard error, not a silent skip. A contradictory configuration raises rather than
guessing. When something is wrong we want a loud, immediate, precise failure — never a quiet
degradation that hides the real problem until it is expensive.

---

## The contracts are tiny and pure

The whole system hangs off three small interfaces — `SandboxRuntime`, `Conduit`, `SandboxPool` —
plus a platform-**neutral** launch spec. The spec carries raw intent (bytes, cores, prefixes); each
runtime **privately** renders that intent to its own platform, and no runtime translates another's
notation. Keep these contracts minimal. The power of this substrate is that so much is possible
behind so little surface — protect that.

---

## The test of a change

Before you add something here, ask:

1. Does it keep isolation and rendezvous **orthogonal**, or does it entangle them?
2. Does the program still stay **plain** (no import, no SDK, no awareness)?
3. Does it keep this repo **caller-agnostic** (zero coupling)?
4. Does it fail **fast and loud**, and never degrade isolation or invent a wall-clock timeout?

If all four hold, it belongs. If any fails, the elegant version of your change is still waiting to be
found.

# EVA — Evolutional Agent

![status](https://img.shields.io/badge/status-experimental-orange)
![python](https://img.shields.io/badge/python-3.12-blue)
![sandbox](https://img.shields.io/badge/sandbox-Docker-2496ED)
![license](https://img.shields.io/badge/license-MIT-green)

> [!WARNING]
> **Experimental and self-modifying.** EVA runs arbitrary shell commands and
> rewrites its own source code. **Only ever run it inside the provided Docker
> sandbox** — never directly on a host or against data you care about.

EVA is a small, self-evolving LLM-driven agent. A tiny **immutable kernel** boots a
single **seed** release; from there EVA can rewrite, test, and promote new versions of
*itself* inside a hardened Docker sandbox — every self-change a **gated, reversible**
step, never live surgery on a running system.

![EVA start screen](docs/eva-start.png)

## What EVA is — and isn't

EVA is not a finished agent architecture. It is a **safe architecture for evolving one.**

- **Fixed meta-architecture** (the kernel): seed → candidate → gate → promote →
  ledger → rollback. The agent can never touch it.
- **Evolvable agent-architecture** (the release): the loop, tools, adapters,
  memory, self-model, TUI — all of `seed/v001` is only *generation 0*. EVA
  rewrites copies of it under the gates.

That makes EVA **bounded architecture-agnostic**: not "no architecture", but
"architecture as a gated, reversible, evolvable state." Platform agents ship the
structure — gateway, skill registry, channels, scheduler. EVA ships the
**mechanism to grow one under control**, so two different uses can, over many
generations, grow two different EVAs — without ever weakening the kernel, gates,
or policies.

The seed is deliberately the **smallest viable organism plus an immune system**
(gates, ratchet, policies, manifest hashes), not a product. New organs are
*earned* through use, then optionally folded back into a later seed.

## How EVA differs

Most autonomous-agent projects ship a **fixed architecture** (gateway, skill
registry, planner, memory) and improve via prompts/config. EVA ships the
**mechanism to grow one safely**:

| | Most agent frameworks | EVA |
|---|---|---|
| Architecture | fixed, shipped up front | a generation-0 **seed** EVA can rewrite |
| Self-improvement | prompt/config tweaks | rewrites its own **code** as a gated release |
| Safety of self-change | manual / none | ratchet + independent **kernel gate** + rollback ledger |
| New capability | you code a plugin | **earned from real usage**, then promoted |
| First run | keys & services | `EVA_PROVIDER=fake` or local Ollama — one command |

## The idea in one picture

```mermaid
flowchart TB
    subgraph boot["bootstrap · runs once"]
        direction LR
        K["<b>organism.py</b><br/>immutable kernel"] -->|seeds| S["<b>seed/v001</b><br/>genome · hash-pinned"]
        S -->|"verify SHA-256<br/>+ materialize"| R["<b>runtime/releases/v1</b><br/>first live release"]
    end

    R ==> VN

    subgraph loop["evolution loop"]
        direction LR
        VN["runtime/releases/<b>vN</b><br/>live release"] -->|EVA edits itself| C["<b>vN-candidate</b>"]
        C -->|verify| G{"gates<br/>tests · ratchet<br/>kernel_gate"}
        G -->|"pass → promote"| VN1["<b>vN+1</b><br/>new live release"]
        G -->|fail → discard| VN
        VN1 -->|becomes the live vN| VN
        VN1 -.->|rollback via ledger| VN
    end

    classDef immutable stroke:#e11d48,stroke-width:2px;
    classDef gate stroke:#2563eb,stroke-width:2px;
    class K immutable;
    class G gate;
```

- **`organism.py`** is the kernel: ~600 lines, baked into the image, **not**
  editable by the agent. It seeds `v001`, runs the final promotion gate, records a
  release **ledger**, and can **roll back** through it.
- **`seed/v001/`** is the genome — real, reviewable, hash-pinned files. The kernel
  verifies their SHA-256 before materializing them into `runtime/`.
- **`runtime/releases/vNNN/`** are the live, evolvable versions. EVA edits a
  *candidate*, the gates test it, and only then does EVA **swap itself** for it.

## Architecture: one clean seam

The core talks only to **collaborators**, never to a provider or a terminal
directly. That single seam is what keeps EVA portable and lets it rewrite any
layer without re-inventing the loop.

| Module | Responsibility |
|---|---|
| `core.py` | Provider-neutral turn loop + the `Event`/`Tool`/`ToolCall` types. Knows nothing about OpenAI, CLIs or wire formats. |
| `adapters.py` | `ModelAdapter`s: `openai_chat` (native tool calls or a portable JSON-text fallback), `anthropic` (Claude Messages API + prompt caching) and an offline `fake` for tests. |
| `tools.py` | Canonical tools + sandboxed runtime + the explicit **mode-policy table** (who may write/shell/promote). |
| `human.py` | `HumanInterface` + `ApprovalPolicy` (approve `[y/N/f]`; **`f`** reveals the full command) + host clipboard bridge. |
| `session.py` | Append-only event log = the **source of truth**, with image **blobs** kept out of the log. |
| `context.py` | Deterministic, LLM-free **compaction** — sends the full log while it fits, then a condensed view. |
| `self_model.py` | **Generated** self-knowledge: EVA reads its own anatomy/skills/capabilities/policy on demand (it is not preloaded). |
| `tui.py` | The status view — human-readable, on-the-fly "what is EVA doing now". |
| `agent.py` | Wiring + the CLI/chat loop; the four modes; the friction backlog and improve-pivot. |
| `supervisor.py` | Release gates: required files, the **ratchet**, smoke, dry-runs, qualification rounds. |
| `tests.py` | LLM-free checks — the ratchet itself (every promotion runs them). |
| `evals.py` | **Golden traces**: recorded provider responses replayed through the *real* adapters + loop + runtime (offline, in the gate) — plus an opt-in live smoke. |

Because the self-model is *generated from the live code*, EVA always knows the
release it is actually running and its current toolset — call `inspect_self`
(`overview` · `anatomy` · `skills` · `capabilities` · `policy`).

## Modes

`work`, `improve`, and `review` run as an **interactive chat**: started without a
task, EVA asks for your first message; reply after each turn; empty line or
`exit` ends it.

| Command | What it does |
|---|---|
| `work [task]` | Useful work in `workspace/`. Can inspect itself, but **never** edits its own code. |
| `review [task]` | Read-only inspection — no writes, no evolution. |
| `improve [task]` | **Directed** self-change — builds a gated candidate that implements *your* task. |
| `evolve [N]` | **Autonomous** self-change — EVA picks the improvement, announces it, then implements N rounds. |
| `<mode> resume` | Continue your previous (interactive) session — each mode keeps its own. |
| `status` / `rollback` | Show the active/last-good release / roll back along the ledger. |
| `reseed` | Re-seed `v001` from `seed/` after editing the genome (no rebuild — the seed is mounted). |

## How it evolves

The clip below is a single **`improve`** run, unedited. Asked to *"design and implement
a skill registry"*, EVA reads its own code (`inspect_self`, `read_file`), clones the
active release into a safe **candidate** (`make_candidate`), writes a new
`skill_registry.py` **plus tests**, runs the candidate's suite (`run_tests`), and only
then **requests promotion** — which the supervisor + kernel gate before anything goes
live. A real self-change, end to end, under the guardrails.

![EVA improving itself — inspect → candidate → write + test → gated promotion](docs/Eva-improve.gif)

EVA self-changes in **two modes**, and both land the same way — a
**candidate → tests → gated promotion**. They differ only in *who picks the change*:

- **`improve` — directed.** You hand EVA one concrete task (the run above:
  *"design and implement a skill registry"*). It implements exactly that as a
  candidate — nothing else — verifies it, and requests promotion.
- **`evolve` — autonomous.** No task is given. EVA picks *one* small, high-value
  improvement to its own release, announces what it will change and why in a
  sentence, then builds it as a candidate the same way.

You steer `improve`; EVA steers `evolve` — but neither ever mutates the running
code. Every self-change is a gated, reversible candidate.

Every session feeds a persistent **friction backlog** (`data/state/backlog.jsonl`):
real shell/model failures are recorded with an error-specific signature.
Usefulness is *grown from real failures*, not designed up front. When the same
friction recurs (default 3×), EVA offers to **pivot** to an `improve` cycle aimed
at the root cause — a clean phase change, never live mutation.

Beyond failures, EVA also learns from **user needs**: when a request reveals a
capability it lacks (or can only do awkwardly), it records it with a stable
signature (`note_evolution_need`). A one-off is just handled; only a need that
**recurs** — tracked across sessions — is proposed as a real skill to build and
promote. New abilities are *earned from what you actually keep asking for*.

A self-change only goes live after it clears the gates — the LLM-free checks
(including **golden traces** that drive the real provider adapters over recorded
responses) and three standing guarantees:

1. **The ratchet** — a fix must add/strengthen a test; a candidate may never run
   *fewer* checks than the current release (counted by what actually executes,
   not by source text).
2. **Multi-level rollback** — every promotion is recorded in the release ledger,
   so `rollback` can step back more than one version.
3. **A constitution in the immutable kernel** — `kernel_gate` independently
   verifies a candidate keeps EVA's core identity (a friction memory + a
   self-improvement path) and rehashes the promoted release's manifest so it can
   never lie about its own content. These checks live where the agent can't edit
   them.

## Contained, not "safe"

Containment limits the *blast radius*; it does not make EVA trustworthy. EVA runs
inside a hardened container ([`docker-compose.yml`](docker-compose.yml)):

- non-root user, `cap_drop: ALL`, `no-new-privileges`, CPU/memory/PID limits
- **read-only root filesystem**; only `./data/{runtime,state,workspace,local}` are writable
- the kernel is baked into the image and **not** mounted — the agent can't touch it
- secrets come from `.env` at runtime, never baked into the image

Within that box EVA can extend its **own** runtime tooling without an image change:
a persistent writable HOME (`/eva/.local`) for `pip install --user`, static
binaries on `PATH`, and HTTP via Python `urllib` or `node` `fetch` (there is no
`curl`/`wget`). It **cannot** change the image or `organism.py`.

**Safe vs free sandbox — you choose.** By default EVA runs in the **safe** sandbox above.
For tasks that need system packages (a browser engine's libraries, `apt`), run the **free**
sandbox — `.\run.ps1 -Free <cmd>` / `./run.sh --free <cmd>` — which layers
[`docker-compose.free.yml`](docker-compose.free.yml): a **writable root filesystem, root and
`apt`**. That is a bigger blast radius (root *inside* the container), still contained to the
container and `./data`, and ephemeral (system installs last only for the session). EVA is
told which mode it is in (`EVA_SANDBOX`), so in safe mode it recognises a system-library need
as an *image need* and stops instead of thrashing `apt`. Prefer safe unless you need it.

> **Residual risk:** the container has outbound network access (for the LLM API).
> For maximum isolation, point EVA at a local model and restrict egress.

## Quickstart

**Prerequisites:** Docker Desktop (Linux engine).

```powershell
.\run.ps1 build                  # build the hardened sandbox image
.\run.ps1                        # start EVA — first run sets up .env interactively
```

On the first run without a `.env`, the wrapper interviews you (provider → model from a
numbered menu → masked API-key entry) and writes `.env`; if `.env` exists but a required
value is missing, only that is asked. Prefer to do it by hand? Copy `.env.example` to
`.env` (every option documented) and edit it. Skip the wizard with `EVA_NO_SETUP=1`.

On Linux/macOS use `./run.sh` with the same commands. Other useful ones:

```powershell
.\run.ps1 improve "add a CHANGELOG and report it in work mode"   # directed self-change
.\run.ps1 evolve 3 --yes --allow-shell                           # hands-off (Docker contains it)
.\run.ps1 status        # active / last-good release
.\run.ps1 rollback      # step back along the release ledger
```

**Approvals.** Risky actions prompt `Approve shell? [y/N/f]` — press **`f`** to
reveal the full command/diff before deciding. Set `EVA_TUI_FULL=1` to expand
commands and output in the live view.

**Images.** With a vision-capable model, reference a local image path, Markdown
`![](shot.png)`, or type `/paste` for your latest screenshot. On Windows,
`run.ps1 work`/`improve` auto-stages clipboard screenshots (`Win+Shift+S` →
`/paste`); on the host, `/paste` reads the clipboard directly. Images are
externalized to `data/state/blobs/` to keep the event log small.

**Sessions.** Each `work` run is its own isolated session under
`data/state/sessions/work/<id>/` (own event log + image blobs). The start screen lists
resumable sessions; continue one with `work resume` (most recent) or `work resume <id>`,
and `work --list` shows them all. On resume EVA replays the prior conversation so you can
pick up the thread. `improve`/`review`/`evolve` stay single + mode-keyed, and
self-evolution is serialized by a kernel lock (`unlock` clears a dead one).

**Providers.** The core is provider-neutral; pick the adapter in `.env`:

| `EVA_PROVIDER` | Backend |
|---|---|
| `openai_chat` (default) | Any OpenAI-compatible Chat Completions endpoint (OpenAI, Azure, Ollama, LM Studio, vLLM, OpenRouter). |
| `anthropic` | Anthropic Claude (Messages API) with native tool use and prompt caching (`EVA_MAX_TOKENS`, `EVA_PROMPT_CACHE`). |
| `fake` | Offline, deterministic — for smoke tests / dry runs (no key). |

`EVA_TOOL_MODE` selects `native` (function calling, default) or `json_text`
(portable fallback).

## Inspecting & resetting

Everything EVA does is persisted on the host under `./data/` (git-ignored):
`workspace/` (work product), `runtime/releases/` (every release + `CURRENT`/
`LAST_GOOD`), and `state/` (event log, blobs, friction backlog, supervisor/kernel
ledgers). Delete `data/` or run `reseed` to start fresh — the kernel re-seeds
`v001` from `seed/v001/`, the source of truth. **Evolution lives only in
`data/`** — back valuable changes into `seed/` (and commit) or a reseed loses them.

```
organism.py          immutable kernel (seed · gates · promote · ledger · rollback)
seed/v001/           the genome (layered, hash-pinned), baked into the image
  core.py adapters.py tools.py human.py session.py context.py
  self_model.py tui.py agent.py supervisor.py tests.py evals.py manifest.json
Dockerfile           hardened, non-root image (kernel + seed + Node.js)
docker-compose.yml   the sandbox (read-only fs, caps dropped, resource limits)
run.ps1 / run.sh     wrappers (build/work/improve/review/evolve/paste/reseed/rollback/status)
data/                created at runtime; all evolution lives here (git-ignored)
```

## Honest limitations

A research experiment, not production software.

- The standing gate runs offline: golden traces replay *recorded* provider
  responses, and a live check exists but is opt-in — so a provider silently
  changing its wire format won't fail the gate until you run it.
- The ratchet counts *executed* checks, but still can't prove a test body wasn't
  weakened in other ways.
- Context compaction is deterministic and rudimentary.
- Network egress is open by default (see residual risk above).

## License

[MIT](LICENSE). Have fun, be careful, and don't run it outside the sandbox.

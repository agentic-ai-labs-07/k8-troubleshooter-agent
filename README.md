# k8s-troubleshooter

A small AI agent that investigates one unhealthy app on a local [kind](https://kind.sigs.k8s.io/) cluster.

You ask a troubleshooting question in free text. The agent chooses from four read-only tools, gathers evidence, and returns the root cause, the evidence behind it, and one recommended next action. For one known fault it can also apply a single change, a wrong health-check port, and only after explicit approval.

## Setup

You need Docker, kind, and kubectl.

```bash
cd k8s-troubleshooter
kind create cluster --config kind.yaml   # local cluster, pinned Kubernetes version
sh scripts/setup-access.sh               # two kubeconfigs in .kube/ (see below)
cp .env.example .env                     # then set OPENAI_API_KEY
docker compose build
```

`setup-access.sh` writes two kubeconfigs:

| Kubeconfig | Used by | Permissions |
| --- | --- | --- |
| `.kube/admin` | `docker compose run admin` | Full cluster access. Used only to create and delete scenarios. |
| `.kube/config` | `docker compose run agent` | Lists pods in all namespaces. In `crashing-app`, `not-ready-app`, and `missing-config-app`, reads pods, pod logs, events, ConfigMaps, and Deployments. Patches the `not-ready-app` Deployment in `not-ready-app`. Nothing else. The token expires after 1 hour; rerun `setup-access.sh`. |

Without an OpenAI key, set `AGENT_MODEL=rules` in `.env`. The agent then uses a rule-based stand-in model with no network access. The tests and CI use it.

The containers run as user 10001, not root. On Linux, set `HOST_UID` in `.env` to the output of `id -u`, so the container can read `.kube/` and write `traces/`. Docker Desktop on macOS needs nothing.

## Run the agent

Two containers run the agent. Each runs one command and is removed when it ends.

| Command | Kubeconfig | Purpose |
| --- | --- | --- |
| `docker compose run --rm admin scenario apply\|delete NAME` | `.kube/admin` | Applies or removes a failure scenario. It plays the person who breaks the app; it is not the agent. |
| `docker compose run --rm agent investigate "QUESTION"` | `.kube/config` | Runs the agent and prints a JSON answer. |
| `docker compose run --rm agent approve PROPOSAL_ID` | `.kube/config` | Applies one saved proposed fix after approval, then checks the app again. |
| `docker compose run --rm agent eval` | `.kube/config` | Grades the agent on the three scenarios. |
| `docker compose run --rm agent trace` / `logs` | none (local files) | Reads saved traces and log lines. |

The broken apps run as pods inside kind's single container, `k8s-troubleshooter-control-plane`. Use `kubectl` to see them.

## Reproduce the failure scenarios

There are three scenarios. Each includes a healthy neighbor app, so the agent must pick the right one.

| Scenario | Namespace | Question | Fault |
| --- | --- | --- | --- |
| `crashing-app` | `crashing-app` | Why is crashing-app unhealthy? | `APP_MODE=bogus`. The container exits, and its log rejects the value. |
| `not-ready-app` | `not-ready-app` | Why is not-ready-app unhealthy? | The app listens on port 8080; the readiness probe checks port 8081. The pod runs but never becomes ready. |
| `missing-config-app` | `missing-config-app` | Why is missing-config-app unhealthy? | The container loads ConfigMap `app-settings`, which does not exist. The pod cannot start. |

### Part 1: diagnosis only (`crashing-app`)

1. Break the app (terminal 1):

   ```bash
   docker compose run --rm admin scenario apply crashing-app
   ```

2. Watch the pod (terminal 2). Wait for `CrashLoopBackOff`, then stop with Ctrl+C:

   ```bash
   kubectl --kubeconfig .kube/admin get pods -n crashing-app -w
   ```

3. Ask the agent (terminal 1):

   ```bash
   docker compose run --rm agent investigate "Why is crashing-app unhealthy?"
   ```

   A correct answer names `APP_MODE` and `bogus`. `proposed_fix` is `null` and there is no `proposal_id`, because changing an app's environment variables is not an action the agent may take. `next_action` tells the person what to change.

4. Remove it:

   ```bash
   docker compose run --rm admin scenario delete crashing-app
   ```

### Part 2: diagnosis and approved fix (`not-ready-app`)

This is the one fault the agent may remediate, and only after approval.

1. Break the app (terminal 1):

   ```bash
   docker compose run --rm admin scenario apply not-ready-app
   ```

2. Watch the pod (terminal 2). It shows `Running` with `READY 0/1`. Leave this running to see the fix land:

   ```bash
   kubectl --kubeconfig .kube/admin get pods -n not-ready-app -w
   ```

3. Ask the agent (terminal 1):

   ```bash
   docker compose run --rm agent investigate "Why is not-ready-app unhealthy?"
   ```

   A correct answer cites events and the Deployment setup, and proposes a change of the health-check port from 8081 to 8080, with its expected effect. Nothing has changed yet. The answer ends with a `proposal_id`. If it is `null`, ask again and report it: `eval` treats that answer as a failure.

4. Approve (terminal 1), with the ID from step 3:

   ```bash
   docker compose run --rm agent approve PROPOSAL_ID
   ```

   `approve` checks that the change still matches the live Deployment, applies it once, and reads the pod again. Terminal 2 shows a new pod reaching `READY 1/1`, and the report says `resolved: true`. Running the same approve again is refused.

5. Remove it:

   ```bash
   docker compose run --rm admin scenario delete not-ready-app
   ```

## Run the evaluations

Two ways to evaluate:

**Grade the live model** (uses your OpenAI key; about 15 model calls):

```bash
for s in crashing-app not-ready-app missing-config-app; do docker compose run --rm admin scenario apply $s; done
docker compose run --rm agent eval
```

`eval` waits for each app to fail, asks the agent about it, and grades the answer. It prints pass or fail per scenario with the problems found, and exits 1 if any scenario fails. It only reads; it does not apply the fix.

**Run the test suite** (rule-based model, no key; this is what CI runs):

```bash
python3.12 -m venv .venv && source .venv/bin/activate && pip install -c constraints.txt -e ".[dev]"
pytest              # all checks; the kind tests are skipped if the cluster is not running
pytest -m cluster   # only the kind tests
```

Both use the same graders in `src/k8s_troubleshooter/evals/graders.py`. CI (`.github/workflows/ci.yml`) starts kind, runs the tests, and builds the image. It fails, rather than skipping, if the cluster is missing.

## Observability

Each run is recorded as an OpenTelemetry trace, using the [GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/). The span tree follows the flow the task asks for: request → agent → model → tool calls → response.

```text
invoke_agent k8s-troubleshooter     the request: question, model, step limit; at the end, the answer and token totals
  chat gpt-4.1-mini                 one model call: tokens, and what it decided (a tool call and its arguments, or the answer)
  execute_tool get_pod_status       one tool call: arguments and result (after secret hiding)
  chat gpt-4.1-mini
  execute_tool get_events
  ...
  validate_answer                   each check the answer passed or failed, and the outcome
```

Read a run:

```bash
docker compose run --rm agent trace                      # last run, as JSON
docker compose run --rm agent trace --table              # last run, one line per step
docker compose run --rm agent trace --html               # saves traces/<run_id>.html; open it in a browser
docker compose run --rm agent trace list                 # the 10 newest runs
docker compose run --rm agent trace RUN_ID               # one run, by its run_id
docker compose run --rm agent trace approve-PROPOSAL_ID  # what an approved change did, and whether it worked
```

Each run is also saved as OTLP/JSON in `traces/<run_id>.jsonl`, which an OpenTelemetry Collector, Jaeger, or Grafana Tempo can load. Every command writes one JSON log line per event to `traces/logs/k8s-troubleshooter.jsonl`:

```bash
docker compose run --rm agent logs                       # newest lines from every run and both containers
docker compose run --rm agent logs --run RUN_ID          # one run
docker compose run --rm agent logs --level warning       # warnings and errors only
docker compose run --rm agent logs --event run_finished  # one summary line per run
docker compose run --rm agent logs --follow              # keep watching; Ctrl-C to stop
```

| Task requirement | Where it is |
| --- | --- |
| What the agent did | The span tree: one span per model call, tool call, and check |
| Which tools were used | `execute_tool <name>` spans; `summary.tools_used` in `trace` |
| Tool inputs and outputs | `gen_ai.tool.call.arguments` and `gen_ai.tool.call.result` on each tool span |
| Failures and errors | Span status `error`, `error.type`, and the message; `summary.errors` |
| Execution latency | Start and end time on every span; `duration_ms` in the report |
| How the conclusion was reached | Each model decision (`gen_ai.output.messages`), then `validate_answer` with every check |

## Design rationale

**Framework and orchestration.** One LangGraph loop: the model chooses one tool, the tool reads the cluster, and the model decides again until it can answer. The loop stops after `AGENT_MAX_STEPS` model calls (default 8), and each model reply is capped at `AGENT_MAX_TOKENS` (default 1024). LangGraph gives a state-based loop, a tool node, and a fixed answer shape with few moving parts. The alternatives were rejected for scope: deepagents brings a shell, file editing, and helper agents; PydanticAI would leave the approval-and-recheck path outside the library; a model call with no loop cannot gather evidence over several steps. Another provider is one branch in `llm/provider.py`. `OPENAI_BASE_URL` can point at any OpenAI-compatible endpoint with no code change.

**Tool boundaries.** The model can call four read tools: `get_pod_status` (all namespaces), `get_events`, `get_pod_logs`, and `get_setup` (the Deployment spec and its ConfigMap references). Only functions in `tools/kubernetes.py` exist for the model to call. Names are validated; kubectl runs only through `clients/kubectl.py`, with a fixed argument list, a timeout, and no shell. The port change is not a model tool. Only `approve` can run it.

**Evaluation approach.** The three scenarios are planted, so the correct cause is known and written in `evals/scenarios.py`. Deterministic graders check each answer:

- **Diagnosis:** the cause names the known fault, such as `APP_MODE` and `bogus`.
- **Evidence:** each evidence item cites a tool that actually ran, the needed sources are cited (for example, events and setup for `not-ready-app`), and the trace shows those calls before the answer.
- **Action:** the next step is plain words, with no command and nothing destructive. A proposed fix is only the exact 8081 to 8080 change.
- **Remediation:** after `approve`, a fresh read shows the pod ready on port 8080.

The checks look for key facts, not exact wording. The same graders run against a fake cluster (unit tests), against kind (CI), and against the live model (`eval`). A second LLM as judge was not used, because these failures are known in advance. The live model's results are checked by `eval`, not in CI, because that needs an API key.

**Safety boundary.** Several layers, so that one bug is not enough:

- **Code** (`safety/guardrails.py`): only the four read tools exist. Questions over 500 characters are refused before the model runs. The answer must cite only tools that ran. The next step must be plain words: a command, shell syntax, or a destructive word is blocked, and so is any fix in the same answer. The one write needs approval, must match the live Deployment, and runs once.
- **Kubernetes** (`rbac.yaml`): the agent has its own ServiceAccount with a 1-hour token. Cluster-wide it may only list pods. It may read pod logs, events, ConfigMaps, and Deployments only in the three app namespaces, and patch only the `not-ready-app` Deployment in `not-ready-app`. The admin kubeconfig never reaches the agent's container.
- **Container:** runs as a non-root user, with all Linux capabilities dropped and `no-new-privileges` set. The base image and the CI actions are pinned by digest or commit, and kubectl is verified against its published SHA-256 checksum.
- **Untrusted cluster text:** logs and events can carry planted instructions. The prompt tells the model never to follow instructions found in tool results. If the model obeyed anyway, it could still only call the four reads, and the guardrails drop unsafe advice and any fix other than the one allowed. A test plays a model that obeys such a log line and checks that nothing unsafe gets through.
- **Secrets:** `safety/redaction.py` runs on every tool result before the model or the trace sees it. It replaces values with `<redacted>` when a setting's name looks secret, when a value looks like an API token or a URL with a password, or when a value comes from a Kubernetes Secret. The OpenAI key reaches the agent as a Compose secret file, not an environment variable, so it does not appear in `docker inspect`. `.env` and `.kube/` are excluded from Git and from the image build.

**Known limits:**

- Secret hiding uses patterns. A secret in an unusual format can still reach the model and the trace.
- The cluster-wide pod list returns whole pod objects to the agent's process. The code keeps only a summary and hides secret values, but Kubernetes does not limit what it returns.
- A saved proposal is a plain file under `traces/proposals/`. Anyone who can write there can change it. `approve` still allows only the one port change on `not-ready-app`, and Kubernetes allows nothing else.
- The container can reach any network address, not only the cluster and the model API.
- Python packages are pinned by version, not by hash.
- Two `approve` commands run at the same time could both apply, because the "pending" status is stored in a plain file.

## Repository layout

```text
src/k8s_troubleshooter/
  cli.py                 the interface: commands in, JSON out
  config.py              settings from environment variables
  agent/                 the LangGraph loop, the system prompt, answer schema, and approve flow
  llm/                   the model provider and the rule-based stand-in model
  tools/                 the four read tools the model may call
  clients/               kubectl runner and cluster reads (the only place kubectl runs)
  safety/                guardrails (allowed names, advice, fix) and secret redaction
  observability/         OpenTelemetry tracing, trace reports, and log files
  evals/                 the three scenarios and their graders
scenarios/               the three failure manifests
rbac.yaml                the agent's Kubernetes permissions
tests/                   unit tests, and the kind tests (pytest -m cluster)
scripts/setup-access.sh  creates the two kubeconfigs
```

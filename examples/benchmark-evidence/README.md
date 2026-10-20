# Compare a harness against its baseline

Harneloop records a benchmark protocol, binds a candidate before held-out results,
and verifies that both arms cover the same tasks and trials. Your executor runs
the target agent and your grader evaluates its artifacts. No model provider,
benchmark dataset or media dependency is installed with this workflow.

Start with `harneloop onboard` and map the actual environment in your unit. Keep
grader answers unavailable to the solving agent. Use development tasks to build
candidates, then evaluate a frozen candidate on held-out tasks without editing it.

## Freeze the protocol

Save a JSON manifest with these fields. The all-zero revisions below are format
placeholders: replace them with the actual immutable source and grader commits
or `sha256:<64 hex>` checksums. Extra task fields can record prompt hashes.

```json
{
  "source": {"name": "your-task-suite", "revision": "0000000000000000000000000000000000000000"},
  "grader": {"name": "your-grader", "revision": "0000000000000000000000000000000000000000"},
  "tasks": [
    {"task_id": "dev-1", "split": "dev"},
    {"task_id": "test-1", "split": "test"}
  ],
  "repeats": 1,
  "seed": 17,
  "executor": "your-executor",
  "model": "your-exact-model-id",
  "effort": "your-setting-or-provider-default",
  "settings": {"temperature": 0},
  "tools": {"allowed": ["your-tool"]},
  "budget_policy": {"max_turns": 20, "timeout_seconds": 600}
}
```

```sh
harneloop benchmark create ./unit pilot --manifest manifest.json
harneloop benchmark bind ./unit pilot --candidate-id cand-0003
harneloop benchmark show ./unit pilot
```

`bind` requires a ready or validating candidate. It freezes that candidate's file
contents and modes; it can only happen once and before any result is recorded.
Changing the overlay requires a new protocol. Protocol and result records live
under `.evolve/benchmarks/`, outside candidate-editable material and thin exports.
The seed describes your selection or execution plan; it does not make a remote
model deterministic.

## Run both arms and capture evidence

Create a distinct run for every held-out task, trial and arm. Trial numbers begin
at zero. Baseline runs use the current harness without a candidate; candidate
runs use the frozen overlay through your executor's normal integration.

```sh
harneloop run start ./unit --task "Baseline test-1" --benchmark-id pilot --benchmark-task-id test-1 --benchmark-trial 0 --benchmark-arm baseline
harneloop run start ./unit --task "Candidate test-1" --candidate-id cand-0003 --benchmark-id pilot --benchmark-task-id test-1 --benchmark-trial 0 --benchmark-arm candidate
```

Run the executor and grader with the same model, tools, settings and budget for
both arms. Attach their outputs with `harneloop artifact add`, then finish
each run with `harneloop run finish`. Use `--help` for those commands' required
arguments. Execution success only means the executor finished; a failed grader
score is still recorded separately.

Save each result as JSON. Copy `observed_conditions` exactly from the six manifest
fields shown below, using actual observed settings. Replace run and artifact IDs
with the IDs produced by your run. Repeat for every task, trial and arm.

```json
{
  "observed_conditions": {
    "executor": "your-executor",
    "model": "your-exact-model-id",
    "effort": "your-setting-or-provider-default",
    "settings": {"temperature": 0},
    "tools": {"allowed": ["your-tool"]},
    "budget_policy": {"max_turns": 20, "timeout_seconds": 600}
  },
  "arm": "baseline",
  "task_id": "test-1",
  "trial": 0,
  "run_id": "run-0001",
  "score": 1,
  "artifact_ids": ["artifact-0001"],
  "status": "scored"
}
```

```sh
harneloop benchmark record ./unit pilot --result result.json
harneloop benchmark compare ./unit pilot --candidate-id cand-0003
```

Scores range from 0 to 1; higher is better. A scored zero is an evaluated failure.
Use `status: "failed"`, score `0` and a concrete `reason` for an execution failure.
Use `status: "unavailable"`, score `null` and a reason for an unavailable evaluation.
These rows remain in coverage counts. Optional `metrics` can contain measured
`duration_seconds`, `input_tokens`, `output_tokens` and `cost_usd`; omitted values
remain unknown, rather than becoming zero.

Comparison rejects missing pairs, duplicate cells, reused run IDs, condition
drift, changed candidate files and changed stored artifacts. It reports per-task
deltas, wins, losses, ties and unavailable evaluations. It does not promote a
candidate. Choose promotion using the unit's normal validation process and the
scope of the evidence; a tie or small pilot does not establish an improvement.

## Compose candidates and retain lessons

```sh
harneloop refine compose ./unit --candidate-id cand-0001 --candidate-id cand-0002 --summary "Combine compatible improvements"
harneloop refine learn ./unit --cue "boundary inputs" --lesson "Check both sides of the boundary" --run-id run-0001 --artifact-id artifact-0001
harneloop refine lessons ./unit --query "boundary"
```

Composition unions compatible file overlays from open candidates on one current
base and one plane. Conflicting bytes or modes require an explicit resolution.
It preserves source candidates and the strongest validation tier, and carries no
validation evidence into the new candidate: validate the composition afresh.

Lessons are operator-authored observations linked to finished runs and captured
artifacts. Retrieval checks their hashes again. They are explicit context you
can select for an agent, rather than automatically injected universal rules.

New candidate runs and evidence records fingerprint the overlay. Evidence-backed
promotion rechecks those fingerprints and captured artifact hashes; edits need
fresh evidence. Legacy records without fingerprints remain readable. Checksums
detect ordinary drift, but a local operator can rewrite records and checksums.
Harneloop does not authenticate remote execution settings, prove task isolation,
or replace an executor sandbox or independent grader.

# Choosing by history

When several targets declare the same capability, [failover](failover.md)
decides *who could* carry out a step. This decides *in what order they are
asked* — from what they have actually been doing, rather than from the order
someone happened to write the configuration in.

## What it looks like

Three code generators. `KIO7` is not running, `SlowGen` works but takes a
second, `FastGen` works in milliseconds. The plan always names `KIO7`.

```text
koşu  1: KIO7 → SlowGen    ok  3033 ms
koşu  2: SlowGen           ok  1018 ms
koşu  3: FastGen           ok     2 ms     ← an exploratory turn
koşu  4: SlowGen           ok  1015 ms
koşu  5: SlowGen           ok  1019 ms
koşu  6: FastGen           ok    18 ms     ← FastGen has earned the lead
koşu  7: FastGen           ok    16 ms
...
koşu 16: FastGen           ok     2 ms

  FastGen  12/12  rate=0.929  mean=10 ms
  KIO7      0/4   rate=0.167  mean=—
  SlowGen   4/4   rate=0.833  mean=1017 ms
```

Nobody configured that order. The orchestrator worked it out.

## Turning it on

```json
"dispatch": {
    "selection": {
        "enabled": true,
        "window": 20,
        "explore_every": 10
    }
}
```

| Field | Meaning |
|-------|---------|
| `enabled` | Whether history reorders anything. Off by default: candidates stay in the order failover produces them. |
| `window` | How many recent outcomes are kept per target **and capability**. A target is judged on what it has been doing lately, not on everything it ever did. |
| `explore_every` | How often the least-sampled candidate is put first anyway. `0` never explores — see below for why that is worse than it sounds. |

## The ranking

Four keys, in order. It is deliberately simple, because a ranking nobody can
explain is worse than none.

**1. Smoothed success rate**, `(successes + 1) / (attempts + 2)`, rounded to
one decimal.

The smoothing stops one lucky run from beating a long record: one-from-one
gives 0.667, nineteen-from-twenty gives 0.909. It also puts a target with no
history at exactly **0.5** — above one that has been failing, below one that
has been working — which is what lets a newly registered agent get tried at
all without a special case for it.

The rounding matters just as much. Without it, six perfect runs (0.875) beat
three perfect runs (0.8) forever, and the faster of two equally reliable
targets could never overtake the one that simply has more samples. Rates
within about a tenth count as equally reliable.

**2. Mean duration of its successes.** Between two targets that both work,
the faster one goes first. Failed attempts do not count towards it — a call
that timed out says nothing about how fast the target is when it works.

**3. The agent the plan named.** A genuine tie never moves the step.

**4. Registry order.** So the result is stable rather than arbitrary.

Scores are kept per **capability**, never pooled. A target can be reliable at
one thing and unreliable at another, and mixing them would hide both.

## Why it has to explore

A target with one success outranks every target with none. Left alone, that
means:

- a newly registered agent is never asked, so never earns a record, so is
  never asked;
- a target tried once has too few samples to ever catch up with the
  incumbent, however much faster it is.

So every `explore_every` decisions for a capability, the **least-sampled**
candidate is put first instead. Least-sampled rather than untried, because
one turn is not enough evidence to compete — an agent has to be asked
repeatedly before its rate can rise to the leader's.

It is a counter, not a coin toss: the same sequence always behaves the same
way, which matters for a system you are trying to debug. And a bad
exploratory guess costs one extra call, because failover catches it.

In the run above, `explore_every` was 3 — deliberately aggressive, to make it
visible. The default of 10 spends a tenth of the traffic finding out whether
the current favourite is still the right one.

## Where the history lives

In the process, and only there.

Telemetry is fire-and-forget to a collector and cannot be read back. The
Session Manager is a separate service, and querying it before every routing
decision would put a network round trip and a hard dependency in the middle
of dispatch. So outcomes are recorded as they happen, in memory.

One scoreboard is shared for the life of the process: per session in the
terminal application, across all requests in the Workflow API. **It does not
survive a restart** — a long-running service accumulates useful history, a
short terminal session mostly does not.

## Seeing it

The registry listing carries each target's record per capability:

```bash
curl -s localhost:8101/registry/agents -H "Authorization: Bearer $KIO1_REGISTRY_TOKEN" | jq
```

```json
{
  "agent_id": "FastGen", "status": "ready", "origin": "registration",
  "history": [
    {"capability": "code_generation", "attempts": 12, "successes": 12,
     "rate": 0.929, "mean_success_ms": 10}
  ]
}
```

A reorder is logged when it happens, naming which target moved ahead of which.

## How this fits with health checks

They answer different questions. [Health checks](live-directory.md) answer
*is it there*, and remove a target from the registry entirely when it is not.
Selection answers *is it any good*, among the ones that are there.

With health checks on, a permanently broken agent is withdrawn and stops
consuming exploratory turns at all. With them off, it keeps costing one
failed call every `explore_every` decisions — which is the price of noticing
if it ever comes back.

## Limits

- **Nothing survives a restart.** Seeding the scoreboard from the Session
  Manager's recorded steps would fix that, and the data is already there.
- **Success is binary.** A step that succeeded counts the same whether the
  agent produced something excellent or something barely usable. `verdict` is
  not read here.
- **Cost and energy are not considered.** Only reliability and wall-clock
  time. The telemetry for a richer measure exists; the ranking does not use
  it yet.
- **One process, one view.** Two KIO1 instances behind a load balancer learn
  separately and may disagree about who is best.

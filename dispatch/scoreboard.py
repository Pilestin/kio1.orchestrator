"""What each target has actually done, and what that says about who to ask.

Failover decides *who else could* carry out a step. This decides *in what
order to ask them*, from what happened last time rather than from the order
someone happened to write the configuration in.

The data has to be kept here. Telemetry is fire-and-forget to a collector and
cannot be read back; the Session Manager is a separate service, and querying
it before every routing decision would put a network round trip and a hard
dependency in the middle of dispatch. So outcomes are recorded as they
happen, in this process.

The ranking is deliberately simple, because a ranking nobody can explain is
worse than none:

1. **Smoothed success rate**, `(successes + 1) / (attempts + 2)`, rounded to
   one decimal. A target with no history sits at exactly 0.5 — above one that
   has been failing, below one that has been working. That is what lets a
   newly registered agent get tried at all without a special case for it.
   Rounding matters: without it, a target with six perfect runs (0.875)
   outranks one with three (0.8) forever, and the faster of two equally
   reliable targets could never overtake the one that simply has more
   samples. Rates within about a tenth count as equally reliable.
2. **Mean duration of its successes.** Between two targets that both work,
   the faster one goes first.
3. **The agent the plan named**, so a genuine tie never moves the step.
4. **Registry order**, so the result is stable rather than arbitrary.

That alone would never discover anything. A target with one success outranks
every target with none, so a newly registered agent — possibly a much better
one — is never asked and never earns a record. And a target tried once has
too few samples to ever catch up with the incumbent.

So every `explore_every` decisions for a capability, the **least-sampled**
candidate is put first instead. Least-sampled rather than untried, because
one lucky turn is not enough evidence to compete: an agent has to be asked
repeatedly before its rate can rise to the leader's. It is a counter rather
than a coin toss, so the same sequence always behaves the same way, and
failover means a bad guess costs one extra call.
"""

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

from dispatch.agent_registry import AgentEndpoint

logger = logging.getLogger(__name__)

# The rate a target with no history is treated as having. Fixed by the
# smoothing formula rather than chosen: (0 + 1) / (0 + 2).
UNPROVEN_RATE = 0.5

# Decimal places the rate is compared at. Two targets whose rates round the
# same way are treated as equally reliable, and speed decides between them.
RATE_PRECISION = 1


@dataclass(frozen=True)
class SelectionConfig:
    """Whether and how history reorders the candidates for a step.

    Attributes:
        enabled: Whether history is consulted at all. Off by default, which
            keeps candidates in the order failover produces them: the agent
            the plan named, then everyone else in registry order.
        window: How many recent outcomes per target and capability are kept.
            Bounded so a target is judged on what it has been doing lately
            rather than on everything it ever did.
        explore_every: How often the least-sampled candidate is put first
            anyway, in decisions per capability. `0` never explores, which
            means the ranking can only ever confirm what it already believes:
            a target that has never been asked is reached solely when
            everything with a record has failed.
    """

    enabled: bool = False
    window: int = 20
    explore_every: int = 10


@dataclass(frozen=True)
class Outcome:
    """One recorded attempt."""

    ok: bool
    duration_ms: int


@dataclass(frozen=True)
class Score:
    """What one target's recent history says about it, for one capability.

    Attributes:
        agent_id: The target.
        capability: The capability these outcomes are for. A target can be
            reliable at one thing and not another, so they are never mixed.
        successes: Successful attempts inside the window.
        attempts: Attempts inside the window.
        mean_success_ms: Mean duration of the successful ones, or None when
            there are none.
    """

    agent_id: str
    capability: str
    successes: int
    attempts: int
    mean_success_ms: float | None

    @property
    def rate(self) -> float:
        """Return the smoothed success rate.

        Returns:
            `(successes + 1) / (attempts + 2)`. Smoothed rather than raw so a
            single success does not outrank a long record: one-from-one gives
            0.667, nineteen-from-twenty gives 0.909.
        """
        return (self.successes + 1) / (self.attempts + 2)

    def to_summary(self) -> dict[str, Any]:
        """Return a JSON-safe view for the registry API."""
        return {
            "capability": self.capability,
            "attempts": self.attempts,
            "successes": self.successes,
            "rate": round(self.rate, 3),
            "mean_success_ms": (
                round(self.mean_success_ms) if self.mean_success_ms else None
            ),
        }


class Scoreboard:
    """Records what targets did and orders candidates by it.

    Safe to use from several threads: steps in one level are dispatched
    concurrently and all of them report back here.
    """

    def __init__(self, selection: SelectionConfig | None = None) -> None:
        """Create an empty scoreboard.

        Args:
            selection: Whether history is consulted, and how much is kept.
        """
        self._selection = selection if selection is not None else SelectionConfig()
        self._lock = threading.Lock()
        self._history: dict[tuple[str, str], deque[Outcome]] = {}
        self._decisions: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        """Report whether this scoreboard reorders anything."""
        return self._selection.enabled

    def record(
        self, agent_id: str, capability: str, *, ok: bool, duration_ms: int
    ) -> None:
        """Record one attempt.

        Every attempt is recorded, including the ones that lost a step to a
        substitute: that a target failed is exactly what the next decision
        needs to know.

        Args:
            agent_id: The target that was asked.
            capability: What it was asked for.
            ok: Whether it succeeded.
            duration_ms: How long the attempt took.
        """
        key = (agent_id, capability)

        with self._lock:
            window = self._history.get(key)

            if window is None:
                window = deque(maxlen=max(1, self._selection.window))
                self._history[key] = window

            window.append(Outcome(ok=ok, duration_ms=duration_ms))

    def score_for(self, agent_id: str, capability: str) -> Score | None:
        """Return what is known about one target and capability.

        Args:
            agent_id: The target.
            capability: The capability.

        Returns:
            Its score, or None when it has never been tried for this.
        """
        with self._lock:
            window = self._history.get((agent_id, capability))
            outcomes = list(window) if window else []

        if not outcomes:
            return None

        successful = [outcome for outcome in outcomes if outcome.ok]

        return Score(
            agent_id=agent_id,
            capability=capability,
            successes=len(successful),
            attempts=len(outcomes),
            mean_success_ms=(
                sum(outcome.duration_ms for outcome in successful) / len(successful)
                if successful
                else None
            ),
        )

    def scores_for_agent(self, agent_id: str) -> tuple[Score, ...]:
        """Return every capability score recorded for one target."""
        with self._lock:
            capabilities = [
                capability
                for (recorded_id, capability) in self._history
                if recorded_id == agent_id
            ]

        scores = [self.score_for(agent_id, capability) for capability in capabilities]

        return tuple(
            sorted(
                (score for score in scores if score is not None),
                key=lambda score: score.capability,
            )
        )

    def rank(
        self,
        candidates: tuple[AgentEndpoint, ...],
        capability: str,
        planned_agent_id: str,
    ) -> tuple[AgentEndpoint, ...]:
        """Return the candidates in the order they should be tried.

        Args:
            candidates: The targets that can carry out the step, in the order
                failover produced them.
            capability: The capability being asked for.
            planned_agent_id: The agent the plan named, used as a tiebreaker
                so a genuine tie never moves the step.

        Returns:
            The same targets, reordered. Returned untouched when selection is
            off or there is nothing to choose between.
        """
        if not self._selection.enabled or len(candidates) < 2:
            return candidates

        def sort_key(item: tuple[int, AgentEndpoint]) -> tuple[float, float, int, int]:
            index, agent = item
            score = self.score_for(agent.agent_id, capability)

            return (
                -round(
                    score.rate if score is not None else UNPROVEN_RATE,
                    RATE_PRECISION,
                ),
                (
                    score.mean_success_ms
                    if score is not None and score.mean_success_ms is not None
                    else float("inf")
                ),
                0 if agent.agent_id == planned_agent_id else 1,
                index,
            )

        ordered = tuple(
            agent for _, agent in sorted(enumerate(candidates), key=sort_key)
        )

        # After ranking, not before: the candidate that would otherwise lead
        # is what an exploratory turn has to be measured against.
        if self._exploring(capability):
            explored = self._explore(ordered, capability)

            if explored is not None:
                return explored

        if ordered[0].agent_id != candidates[0].agent_id:
            logger.info(
                "History reordered the candidates for %s: %s ahead of %s",
                capability,
                ordered[0].agent_id,
                candidates[0].agent_id,
            )

        return ordered

    def _exploring(self, capability: str) -> bool:
        """Report whether this decision is the one that tries something new.

        Args:
            capability: The capability being decided.

        Returns:
            True on every `explore_every`-th decision for this capability.
        """
        if self._selection.explore_every < 1:
            return False

        with self._lock:
            count = self._decisions.get(capability, 0) + 1
            self._decisions[capability] = count

        return count % self._selection.explore_every == 0

    def _explore(
        self, candidates: tuple[AgentEndpoint, ...], capability: str
    ) -> tuple[AgentEndpoint, ...] | None:
        """Put the least-sampled candidate ahead of the rest.

        Args:
            candidates: The candidates in the order the ranking put them.
            capability: The capability being decided.

        Returns:
            The reordered candidates, or None when the least-sampled one is
            already going first and there is nothing to learn by reordering.
        """

        def samples(agent: AgentEndpoint) -> int:
            score = self.score_for(agent.agent_id, capability)

            return score.attempts if score is not None else 0

        # Ties go to the earlier candidate, so exploration is deterministic.
        least = min(candidates, key=lambda agent: (samples(agent),))

        if least.agent_id == candidates[0].agent_id:
            return None

        logger.info(
            "Trying %s for %s to find out more about it (%d samples so far)",
            least.agent_id,
            capability,
            samples(least),
        )

        return (least, *(a for a in candidates if a.agent_id != least.agent_id))

    def summary(self) -> tuple[Score, ...]:
        """Return every score recorded, for reporting."""
        with self._lock:
            keys = list(self._history)

        scores = [self.score_for(agent_id, capability) for agent_id, capability in keys]

        return tuple(
            sorted(
                (score for score in scores if score is not None),
                key=lambda score: (score.agent_id, score.capability),
            )
        )


def load_selection_config(selection_data: Any) -> SelectionConfig:
    """Build a `SelectionConfig` from the `dispatch.selection` block.

    Args:
        selection_data: Decoded `dispatch.selection` object, or an absent value.

    Returns:
        The configured settings.

    Raises:
        ValueError: If the block is malformed.
    """
    if selection_data is None:
        return SelectionConfig()

    if not isinstance(selection_data, dict):
        raise ValueError("dispatch.selection must be a JSON object.")

    enabled = selection_data.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("dispatch.selection.enabled must be a boolean.")

    window = selection_data.get("window", 20)
    if type(window) is not int or window < 1:
        raise ValueError(
            "dispatch.selection.window must be a positive integer, the number "
            "of recent outcomes kept per target and capability."
        )

    explore_every = selection_data.get("explore_every", 10)
    if type(explore_every) is not int or explore_every < 0:
        raise ValueError(
            "dispatch.selection.explore_every must be zero or a positive "
            "integer, the number of decisions between trying something new."
        )

    if enabled:
        logger.info(
            "History-based selection configured: window=%d explore_every=%d",
            window,
            explore_every,
        )

    return SelectionConfig(enabled=enabled, window=window, explore_every=explore_every)

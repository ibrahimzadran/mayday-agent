"""The shape of one eval case."""

from dataclasses import dataclass, field
from typing import Optional

from evals.assertions import Assertion


@dataclass
class Case:
    id: str
    # consent | correctness | safety | degradation
    category: str
    # What the passenger types, in order. Multi-turn because the consent gate
    # is only meaningful across turns.
    turns: list[str]
    # Deterministic hard gates. Any failure fails the case.
    assertions: list[Assertion] = field(default_factory=list)
    # Optional 1-5 rubric scored by a model. Advisory by default: tone is real
    # but it should not turn a correct answer red.
    rubric: Optional[str] = None
    # Minimum judge score before the case is reported as a quality failure.
    min_score: int = 3
    # Injected backend failure for this case only.
    chaos: Optional[dict] = None
    # Included in the small subset CI runs on every push.
    fast: bool = False

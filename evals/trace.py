"""What one eval run recorded.

Assertions read this, not the raw ADK events, so a change in ADK's event shape
touches one file instead of twenty cases.
"""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolCall:
    name: str
    args: dict
    response: Any = None


@dataclass
class Turn:
    user: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    agent_text: str = ""


@dataclass
class Trace:
    case_id: str
    turns: list[Turn] = field(default_factory=list)
    # Set when the run itself blew up (rate limit, crash). Distinct from an
    # assertion failure: the case did not get a fair chance to pass.
    error: Optional[str] = None

    @property
    def all_calls(self) -> list[ToolCall]:
        return [call for turn in self.turns for call in turn.tool_calls]

    @property
    def tool_names(self) -> list[str]:
        return [call.name for call in self.all_calls]

    def calls_to(self, name: str) -> list[ToolCall]:
        return [call for call in self.all_calls if call.name == name]

    def called(self, name: str) -> bool:
        return bool(self.calls_to(name))

    @property
    def final_text(self) -> str:
        for turn in reversed(self.turns):
            if turn.agent_text:
                return turn.agent_text
        return ""

    @property
    def all_text(self) -> str:
        return "\n".join(turn.agent_text for turn in self.turns)

    def transcript(self) -> str:
        lines = []
        for turn in self.turns:
            lines.append(f"PASSENGER: {turn.user}")
            for call in turn.tool_calls:
                lines.append(f"  [tool] {call.name}({call.args})")
            if turn.agent_text:
                lines.append(f"AGENT: {turn.agent_text}")
        return "\n".join(lines)

"""The facts ledger: everything the worker has seen, and everything it believes, with provenance.

Two kinds of entries:
  Observation  raw content the worker saw (a page, a PDF), stored verbatim with its source.
  Fact         a value the model claims, which must quote an observation. `record` refuses a
               fact whose quote is not literally in the cited observation, or whose value is not
               in the quote. That is the anti-hallucination rule: downstream writes may only use
               facts, so every value written to the ERP traces back to text on a real page.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


@dataclass
class Observation:
    id: str
    source: str          # URL or file the content came from
    kind: str            # "page" | "document"
    text: str
    trusted: bool        # internal systems (ERP, wiki) vs external content (mail, vendor docs)
    flags: list[str] = field(default_factory=list)


@dataclass
class Fact:
    id: str
    key: str
    value: str
    observation_id: str
    quote: str
    source: str


class ProvenanceError(ValueError):
    pass


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _compact(s: str) -> str:
    """Drop separators so '1,48,000.00', '148000.00' and 'INR 148000' compare equal."""
    return re.sub(r"[\s,₹]|inr|rs\.?", "", s.lower())


class Ledger:
    def __init__(self) -> None:
        self.observations: dict[str, Observation] = {}
        self.facts: dict[str, Fact] = {}

    def observe(self, source: str, kind: str, text: str, trusted: bool, flags: list[str] | None = None) -> Observation:
        obs = Observation(f"obs{len(self.observations) + 1}", source, kind, text, trusted, flags or [])
        self.observations[obs.id] = obs
        return obs

    def record(self, key: str, value: str, observation_id: str, quote: str) -> Fact:
        obs = self.observations.get(observation_id)
        if obs is None:
            raise ProvenanceError(f"Unknown observation '{observation_id}'. Cite the obs id shown with the content.")
        if _norm(quote) not in _norm(obs.text):
            raise ProvenanceError(
                f"Quote not found verbatim in {observation_id} ({obs.source}). Copy the exact text from the observation."
            )
        if _compact(value) not in _compact(quote):
            raise ProvenanceError(f"Value '{value}' does not appear in the quote '{quote}'.")
        # Re-recording a key replaces the old belief; the trace keeps the history.
        fact = Fact(f"fact{len(self.facts) + 1}", key, value, observation_id, quote, obs.source)
        self.facts[fact.id] = fact
        return fact

    def get(self, fact_id: str) -> Fact:
        if fact_id not in self.facts:
            raise ProvenanceError(f"Unknown fact '{fact_id}'. Record it with record_fact first.")
        return self.facts[fact_id]

    def summary(self) -> str:
        if not self.facts:
            return "(no facts recorded yet)"
        return "\n".join(f"{f.id}: {f.key} = {f.value!r}  [from {f.observation_id}, {f.source}]" for f in self.facts.values())

    def to_state(self) -> dict:
        """Full JSON-safe copy for checkpointing. Unlike to_dict(), never truncates observation text."""
        return {"observations": [asdict(o) for o in self.observations.values()],
                "facts": [asdict(f) for f in self.facts.values()]}

    @classmethod
    def from_state(cls, state: dict) -> "Ledger":
        ledger = cls()
        for o in state.get("observations", []):
            ledger.observations[o["id"]] = Observation(**o)
        for f in state.get("facts", []):
            ledger.facts[f["id"]] = Fact(**f)
        return ledger

    def to_dict(self) -> dict:
        return {
            "facts": [asdict(f) for f in self.facts.values()],
            "observations": [{**asdict(o), "text": o.text[:2000]} for o in self.observations.values()],
        }

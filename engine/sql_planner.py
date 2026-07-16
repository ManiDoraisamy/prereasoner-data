"""High-level orchestration for the final deterministic SQL planning path."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from engine.sql_candidate import ScoredQuery
from engine.sql_learned_rank import RankerModel, rerank_with_promotion_gate
from engine.sql_profile_expansion import ProfileSearchConfig
from engine.sql_proposal_runtime import ProposalSignalProvider
from engine.sql_search import SQLSearcher


@dataclass
class DeterministicSQLPlanner:
    """Compose typed search, profile proposals, and strict-scoped gated ranking."""

    searcher: SQLSearcher
    signal_provider: ProposalSignalProvider | None = None
    rank_model: RankerModel | None = None
    profile_config: ProfileSearchConfig = ProfileSearchConfig()

    def search(
        self,
        question: str,
        question_vector: Sequence[float] | None = None,
    ) -> list[ScoredQuery]:
        signals = (
            self.signal_provider.signals(
                question, self.searcher.schema, question_vector
            )
            if self.signal_provider is not None else None
        )
        candidates = self.searcher.search(
            question,
            semantic_signals=signals,
            profile_config=self.profile_config,
        )
        if self.rank_model is None or self.signal_provider is None:
            return candidates
        return rerank_with_promotion_gate(self.rank_model, question, candidates)

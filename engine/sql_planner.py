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
    profile_config: ProfileSearchConfig | None = None

    def search(
        self,
        question: str,
        question_vector: Sequence[float] | None = None,
    ) -> list[ScoredQuery]:
        baseline = self.searcher.search(question)
        signals = (
            self.signal_provider.signals(
                question, self.searcher.schema, question_vector
            )
            if self.signal_provider is not None else None
        )
        proposed = self.searcher.search(
            question,
            semantic_signals=signals,
            profile_config=self.profile_config,
        ) if signals is not None else ()
        candidates = baseline
        if proposed and baseline:
            fallback = baseline[0]
            candidates = [fallback] + [
                candidate for candidate in tuple(proposed) + tuple(baseline[1:])
                if candidate.sql != fallback.sql
            ][:max(0, self.searcher.max_candidates - 1)]
        elif proposed:
            candidates = list(proposed)
        if self.rank_model is None or self.signal_provider is None:
            return candidates
        from engine.sql_learned_rank import verify_ranker_contract

        verify_ranker_contract(
            self.rank_model,
            proposer_model=self.signal_provider.model,
            adapter_sha256=getattr(
                self.signal_provider.encoder, "encoder_adapter_sha256", None
            ),
            profile_config=self.profile_config,
            pool_size=self.searcher.max_candidates,
        )
        return rerank_with_promotion_gate(self.rank_model, question, candidates)

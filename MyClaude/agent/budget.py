"""成本统计与预算控制（max_cost / max_turns）。"""
from __future__ import annotations

from MyClaude.agent.constants import print_info


class BudgetMixin:
    def get_token_usage(self) -> dict:
        return {"input": self.total_input_tokens, "output": self.total_output_tokens}
    def show_cost(self) -> None:
        total = self._get_current_cost_usd()
        budget_info = f" / ${self.max_cost_usd} budget" if self.max_cost_usd else ""
        turn_info = f" | Turns: {self.current_turns}/{self.max_turns}" if self.max_turns else ""
        cached = self.total_cache_read_tokens
        billed_input = self.total_input_tokens + self.total_cache_creation_tokens + cached
        hit_rate = round((cached / billed_input) * 100) if billed_input > 0 else 0
        cache_info = (
            f"\n  Cache: {cached} read / {self.total_cache_creation_tokens} write ({hit_rate}% of input from cache)"
            if (cached or self.total_cache_creation_tokens) else ""
        )
        print_info(f"Tokens: {self.total_input_tokens} in / {self.total_output_tokens} out{cache_info}\n  Estimated cost: ${total:.4f}{budget_info}{turn_info}")
    def _get_current_cost_usd(self) -> float:
        # Base input $3/Mtok. Cache read is 0.1x, cache write is 1.25x — the
        # fixed multipliers Claude Code uses across every model tier.
        M = 1_000_000
        return (
            (self.total_input_tokens / M) * 3
            + (self.total_cache_read_tokens / M) * 0.3
            + (self.total_cache_creation_tokens / M) * 3.75
            + (self.total_output_tokens / M) * 15
        )
    def _check_budget(self) -> dict:
        if self.max_cost_usd is not None and self._get_current_cost_usd() >= self.max_cost_usd:
            return {"exceeded": True, "reason": f"Cost limit reached (${self._get_current_cost_usd():.4f} >= ${self.max_cost_usd})"}
        if self.max_turns is not None and self.current_turns >= self.max_turns:
            return {"exceeded": True, "reason": f"Turn limit reached ({self.current_turns} >= {self.max_turns})"}
        return {"exceeded": False}

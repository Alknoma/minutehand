"""What a model call costs, as the user declares it: Minutehand knows no model's price and invents none."""

from __future__ import annotations

from pydantic import Field

from minutehand.domain.scenario import Model


class Price(Model):
    """One model's price per million tokens, as its vendor bills it to the user."""

    model: str = Field(description="The model id exactly as a call names it (`model_calls.model`)")
    input_per_million: float = Field(ge=0, description="Per million input tokens")
    output_per_million: float = Field(ge=0, description="Per million output tokens")
    currency: str = "USD"


class Prices(Model):
    """A prices file (`minutehand query --prices`): a call to a model it does not name has no cost."""

    prices: list[Price] = []

    def cost(self, model: str | None, input_tokens: int | None, output_tokens: int | None) -> tuple[float, str] | None:
        """What one call cost, and in what currency; None when the model is not priced or a count is unknown."""
        found = next((p for p in self.prices if p.model == model), None)
        if found is None or input_tokens is None or output_tokens is None:
            return None
        total = (input_tokens * found.input_per_million + output_tokens * found.output_per_million) / 1_000_000
        return total, found.currency

"""What a model call costs, as the user declares it: Minutehand knows no model's price and invents none."""

from __future__ import annotations

from pydantic import Field

from minutehand.domain.scenario import Model


class Price(Model):
    """One model's price per million tokens, as its vendor bills it to the user."""

    model: str = Field(description="The model id exactly as a call names it (`model_calls.model`)")
    input_per_million: float = Field(ge=0, description="Per million input tokens")
    output_per_million: float = Field(ge=0, description="Per million output tokens")
    cache_read_per_million: float | None = Field(
        default=None, ge=0, description="Per million input tokens read from the prompt cache; None: not declared"
    )
    cache_creation_per_million: float | None = Field(
        default=None, ge=0, description="Per million input tokens written to the prompt cache; None: not declared"
    )
    currency: str = "USD"


class Prices(Model):
    """A prices file (`minutehand query --prices`): a call to a model it does not name has no cost."""

    prices: list[Price] = []

    def cost(
        self,
        model: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        *,
        cache_read_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
    ) -> tuple[float, str] | None:
        """What one call cost, and in what currency: `input_tokens` are the uncached ones, at the input price, and
        each cached kind at its own declared price. None when the model is not priced, a count is unknown, or the
        call has cached tokens of a kind whose price the file does not declare: no price is guessed."""
        found = next((p for p in self.prices if p.model == model), None)
        if found is None or input_tokens is None or output_tokens is None:
            return None
        total = input_tokens * found.input_per_million + output_tokens * found.output_per_million
        for count, rate in (
            (cache_read_tokens, found.cache_read_per_million),
            (cache_creation_tokens, found.cache_creation_per_million),
        ):
            if not count:
                continue
            if rate is None:
                return None
            total += count * rate
        return total / 1_000_000, found.currency

from __future__ import annotations

import pandas as pd

from cross_alpha.spec import AlphaSpec
from cross_alpha.strategy import CrossAlphaComputeContext, compute_signal_details
from indicators.pandas.cs_ops import cs_zscore
from indicators.pandas.ts_ops import ts_ema


def combine_members(
    panel: dict[str, pd.DataFrame],
    member_specs: list[AlphaSpec],
    ema_smooth: int,
    context: CrossAlphaComputeContext | None = None,
) -> pd.DataFrame:
    """Combine N member alphas' own scores into one ensemble signal:
    ``ts_ema(mean(cs_zscore(member_score) for member in members), ema_smooth)``.

    Each member is evaluated with its OWN AlphaSpec (own signal/params/
    universe settings) over the SAME shared panel by reusing
    compute_signal_details() -- the same function each member's standalone
    alpha already uses -- so no signal logic is duplicated and each member's
    own tests double as regression coverage for the ensemble's inputs.
    """
    ctx = context or CrossAlphaComputeContext(panel)
    zscored_members: list[pd.DataFrame] = []
    for member_spec in member_specs:
        score, _long_condition, _short_condition, _components = compute_signal_details(
            panel, member_spec, context=ctx,
        )
        if score is None:
            raise ValueError(
                f"ensemble member {member_spec.alpha_id!r} (signal={member_spec.signal!r}) "
                "produced no score -- only score-producing signals can be ensemble members"
            )
        zscored_members.append(cs_zscore(score))
    combined = sum(zscored_members) / len(zscored_members)
    return ts_ema(combined, int(ema_smooth))

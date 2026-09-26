"""Provider-agnostic GPU selection: prefer the requested GPU, then fall back.

Input is the provider's live catalogue (never an invented list). Output is an
ordered candidate list the provider tries in sequence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.domain.models import GpuOffer, GpuRequirements


def _matches(gpu_name: str, wanted: str) -> bool:
    """Exact, case-insensitive token match: ``L40`` never matches ``L40S``,
    ``A10`` never matches ``A100``, but ``A100`` matches ``A100 80GB``."""
    tokens = re.split(r"[^A-Z0-9]+", gpu_name.upper())
    return wanted.upper().strip() in tokens


def _eligible(offer: GpuOffer, req: GpuRequirements) -> bool:
    if offer.gpu_count < 1 or offer.gpu_count > req.max_gpu_count:
        return False
    if offer.vram_per_gpu_gb * offer.gpu_count < req.min_vram_gb:
        return False
    return not (
        req.min_compute_capability is not None
        and offer.compute_capability is not None
        and offer.compute_capability < req.min_compute_capability
    )


def _price(offer: GpuOffer) -> float:
    return offer.price_per_hour if offer.price_per_hour is not None else float("inf")


def rank_offers(offers: Iterable[GpuOffer], req: GpuRequirements) -> list[GpuOffer]:
    """Order: explicit instance types (as configured) → preferred GPU → fallbacks (in the
    configured order) → any other eligible GPU. Cheapest first within each tier."""
    offers = list(offers)
    if req.explicit_instance_types:
        by_type = {o.instance_type: o for o in offers}
        ranked = [
            by_type.get(t)
            or GpuOffer(instance_type=t, gpu_name="unknown", gpu_count=1, vram_per_gpu_gb=0)
            for t in req.explicit_instance_types
        ]
        return ranked[: req.max_candidates]

    eligible = [o for o in offers if _eligible(o, req)]
    tiers = [req.preferred_gpu, *req.fallback_gpus]

    def tier(o: GpuOffer) -> int:
        for index, name in enumerate(tiers):
            if _matches(o.gpu_name, name):
                return index
        return len(tiers)

    eligible.sort(key=lambda o: (tier(o), _price(o), o.boot_time_seconds or 10**6, o.instance_type))
    seen: set[str] = set()
    result: list[GpuOffer] = []
    for offer in eligible:
        if offer.instance_type not in seen:
            seen.add(offer.instance_type)
            result.append(offer)
    return result[: req.max_candidates]

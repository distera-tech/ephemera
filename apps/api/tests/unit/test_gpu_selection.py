from app.application.gpu_selection import rank_offers
from app.domain.models import GpuOffer, GpuRequirements


def offer(
    t: str, gpu: str, vram: float, price: float | None, count: int = 1, cap: float = 8.9
) -> GpuOffer:
    return GpuOffer(t, gpu, count, vram, "p", cap, price, 100)


CATALOG = [
    offer("a10g", "A10G", 24, 1.0),
    offer("l40", "L40", 48, 1.2),
    offer("l40s-b", "L40S", 48, 1.9),
    offer("l40s-a", "L40S", 48, 1.6),
    offer("a100", "A100", 80, 3.0),
    offer("h100x8", "H100", 80, 25.0, count=8),
    offer("v100", "V100", 32, 0.5, cap=7.0),
    offer("a6000", "RTX A6000", 48, 0.9),
]


def req(**kw: object) -> GpuRequirements:
    base: dict[str, object] = {
        "preferred_gpu": "L40S",
        "min_vram_gb": 40,
        "fallback_gpus": ("A100",),
        "min_compute_capability": 8.0,
    }
    base.update(kw)
    return GpuRequirements(**base)  # type: ignore[arg-type]


def test_prefers_requested_gpu_cheapest_first() -> None:
    ranked = [o.instance_type for o in rank_offers(CATALOG, req())]
    assert ranked[:2] == ["l40s-a", "l40s-b"]


def test_fallback_order_then_rest_by_price() -> None:
    ranked = [o.instance_type for o in rank_offers(CATALOG, req())]
    assert ranked[2] == "a100"
    assert set(ranked[3:]) <= {"a6000", "l40"}


def test_l40_does_not_match_l40s_preference() -> None:
    ranked = rank_offers([offer("l40", "L40", 48, 0.1)], req(fallback_gpus=()))
    assert ranked and ranked[0].gpu_name == "L40"  # eligible, but only as "other"
    assert (
        rank_offers([offer("l40", "L40", 48, 0.1), offer("x", "L40S", 48, 5)], req())[0].gpu_name
        == "L40S"
    )


def test_filters_vram_capability_and_multi_gpu() -> None:
    names = {o.instance_type for o in rank_offers(CATALOG, req(max_candidates=20))}
    assert "a10g" not in names  # 24 GB < 40
    assert "v100" not in names  # capability 7.0 < 8.0
    assert "h100x8" not in names  # never silently take an 8-GPU box


def test_no_eligible_offer_returns_empty() -> None:
    assert rank_offers(CATALOG, req(min_vram_gb=200)) == []


def test_explicit_types_bypass_search_order() -> None:
    ranked = rank_offers(CATALOG, req(explicit_instance_types=("a100", "custom.type")))
    assert [o.instance_type for o in ranked] == ["a100", "custom.type"]


def test_max_candidates_cap() -> None:
    assert len(rank_offers(CATALOG, req(max_candidates=2))) == 2

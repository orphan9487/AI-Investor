from .baselines import CapWeighted, EqualWeight, Momentum, RevenueGrowth
from .composite import CompositeScore

REGISTRY = {
    "cap_weighted": CapWeighted,
    "equal_weight": EqualWeight,
    "momentum": Momentum,
    "revenue_growth": RevenueGrowth,
    "composite": CompositeScore,
}

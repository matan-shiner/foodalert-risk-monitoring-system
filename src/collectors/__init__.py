from .fda_enforcement import FDAEnforcementCollector
from .fsis import FSISCollector
from .fsa_uk import FSAUKCollector
from .rasff import RASFFCollector
from .cdc_food_safety_rss import CDCFoodSafetyRSSCollector

COLLECTORS = {
    "fda_enforcement": FDAEnforcementCollector,
    "fsis": FSISCollector,
    "fsa_uk": FSAUKCollector,
    "rasff": RASFFCollector,
    "cdc_food_safety_rss": CDCFoodSafetyRSSCollector,
}

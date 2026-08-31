from .fda_enforcement import FDAEnforcementCollector
from .fsis import FSISCollector
from .fsa_uk import FSAUKCollector
from .rasff import RASFFCollector
from .cdc_food_safety_rss import CDCFoodSafetyRSSCollector
from .cfia_recalls import CFIARecallsCollector
from .fsanz_australia import FSANZAustraliaCollector
from .fsai_ireland import FSAIIrelandCollector
from .sfa_singapore import SFASingaporeCollector

COLLECTORS = {
    "fda_enforcement": FDAEnforcementCollector,
    "fsis": FSISCollector,
    "fsa_uk": FSAUKCollector,
    "rasff": RASFFCollector,
    "cdc_food_safety_rss": CDCFoodSafetyRSSCollector,
    "cfia_recalls": CFIARecallsCollector,
    "fsanz_australia": FSANZAustraliaCollector,
    "fsai_ireland": FSAIIrelandCollector,
    "sfa_singapore": SFASingaporeCollector,
}

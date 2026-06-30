from app.config import settings
from cross_alpha.engine import CrossSectionalEngine


class AlphaEngine(CrossSectionalEngine):
    def __init__(self):
        super().__init__(settings)

from dataclasses import dataclass
from app.domain.states import RuntimeStage


@dataclass
class RuntimeState:
    stage: RuntimeStage=RuntimeStage.BOOT
    blocker: str=""
    cycle_id: str=""
    selected_symbol: str=""
    last_edge_bps: str="0"
    last_confidence: str="0"

    def set(self, stage: RuntimeStage, blocker: str="") -> None:
        self.stage=stage
        self.blocker=blocker

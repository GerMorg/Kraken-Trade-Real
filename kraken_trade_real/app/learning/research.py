from __future__ import annotations

import hashlib
import json
import math
import random
from typing import Callable, Iterable


class ResearchEngine:
    def __init__(self, db: object) -> None:
        self.db=db

    @staticmethod
    def metrics(returns: list[float]) -> dict:
        if not returns:
            return {"samples":0,"return":0.0,"drawdown":0.0,"sharpe":0.0}
        equity=1.0
        peak=1.0
        max_dd=0.0
        path=[]
        for r in returns:
            equity*=1+r
            peak=max(peak,equity)
            max_dd=min(max_dd,equity/peak-1)
            path.append(equity)
        mean=sum(returns)/len(returns)
        var=sum((r-mean)**2 for r in returns)/max(1,len(returns)-1)
        sharpe=mean/math.sqrt(var)*math.sqrt(252) if var>0 else 0.0
        return {"samples":len(returns),"return":equity-1,"drawdown":max_dd,"sharpe":sharpe}

    def walk_forward(self, data: list[dict], scorer: Callable[[list[dict]],list[float]],
                     windows: int=3) -> dict:
        if len(data)<windows*20:
            return {"status":"INSUFFICIENT_DATA","windows":[]}
        size=len(data)//windows
        results=[]
        for idx in range(1,windows+1):
            train=data[:max(1,idx*size-size)]
            test=data[max(1,idx*size-size):idx*size]
            train_hash=hashlib.sha256(json.dumps(train,sort_keys=True,default=str).encode()).hexdigest()
            returns=scorer(test)
            results.append({"window":idx,"train_samples":len(train),"test_samples":len(test),
                            "train_hash":train_hash,"metrics":self.metrics(returns)})
        stable=sum(1 for x in results if x["metrics"]["return"]>0)
        return {"status":"OK","windows":results,"stable_windows":stable,"stability":stable/len(results)}

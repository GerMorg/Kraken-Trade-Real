class CalibrationEngine:
    def evaluate(self,pairs:list[tuple[float,bool]],bins:int=10)->dict:
        if not pairs:return {"sample_count":0,"brier":1.0,"ece":1.0,"bins":[]}
        brier=sum((p-(1 if y else 0))**2 for p,y in pairs)/len(pairs)
        groups=[];ece=0.0
        for i in range(bins):
            lo=i/bins;hi=(i+1)/bins
            rows=[(p,y) for p,y in pairs if lo<=p<hi or (i==bins-1 and p==1)]
            if not rows:continue
            conf=sum(p for p,_ in rows)/len(rows);acc=sum(1 for _,y in rows if y)/len(rows)
            ece+=len(rows)/len(pairs)*abs(acc-conf)
            groups.append({"bin":i,"count":len(rows),"confidence":conf,"accuracy":acc})
        return {"sample_count":len(pairs),"brier":brier,"ece":ece,"bins":groups}

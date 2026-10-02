from __future__ import annotations
from pydantic import BaseModel,Field
import hashlib,json
from typing import Any
class GeminiResult(BaseModel):
    relevance:float=Field(ge=0,le=1); sentiment:float=Field(ge=-1,le=1)
    expected_impact_bps:float=Field(ge=-200,le=200); confidence:float=Field(ge=0,le=1)
    regime_hint:str; topics:list[str]; affected_assets:list[str]; counterargument:str
class GeminiAnalyzer:
    def __init__(self,api_key:str,model:str,enabled:bool,db:Any)->None:
        self.api_key=api_key;self.model=model;self.enabled=enabled;self.db=db;self._client=None
    def _client_for_use(self):
        if not self.enabled or not self.api_key:return None
        if self._client is None:
            from google import genai
            self._client=genai.Client(api_key=self.api_key)
        return self._client
    def analyze(self,news:list[dict[str,Any]],market_context:dict[str,Any])->dict[str,Any]:
        client=self._client_for_use()
        if client is None:return {"status":"DISABLED","effect_bps":0.0}
        prompt=("Research only. Never return an order, trade instruction, position size, leverage instruction "
                "or execution command. Return JSON matching this schema. NEWS="+json.dumps(news[:20],default=str)+
                " CONTEXT="+json.dumps(market_context,default=str))
        try:
            response=client.interactions.create(
                model=self.model,input=prompt,
                response_format={"type":"text","mime_type":"application/json","schema":GeminiResult.model_json_schema()},
                store=False,
            )
            result={"status":"OK",**GeminiResult.model_validate_json(response.output_text).model_dump()}
            digest=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
            self.db.execute("INSERT INTO gemini_analysis(created_at,context_hash,model,result_json) VALUES(?,?,?,?)",
                            (__import__("time").time(),digest,self.model,json.dumps(result,sort_keys=True)))
            return result
        except Exception as exc:
            self.db.event("GEMINI_UNAVAILABLE","WARNING",{"error":type(exc).__name__})
            return {"status":"UNAVAILABLE","effect_bps":0.0}

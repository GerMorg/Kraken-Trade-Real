from __future__ import annotations
from pydantic import BaseModel, Field
import hashlib
import json
from typing import Any
class GeminiResult(BaseModel):
    relevance:float=Field(ge=0,le=1); sentiment:float=Field(ge=-1,le=1)
    expected_impact_bps:float=Field(ge=-200,le=200); confidence:float=Field(ge=0,le=1)
    regime_hint:str; topics:list[str]; affected_assets:list[str]; counterargument:str
class GeminiAnalyzer:
    def __init__(self,api_key:str,model:str,enabled:bool,db:Any,timeout_seconds:int=30)->None:
        self.api_key=api_key
        self.model=model
        self.enabled=enabled
        self.db=db
        self.timeout_seconds=max(5,min(120,int(timeout_seconds)))
        self._client=None
    def _client_for_use(self):
        if not self.enabled or not self.api_key:return None
        if self._client is None:
            from google import genai
            from google.genai import types
            self._client=genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(
                    timeout=self.timeout_seconds*1000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
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
                timeout=float(self.timeout_seconds),
            )
            result={"status":"OK",**GeminiResult.model_validate_json(response.output_text).model_dump()}
            digest=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
            self.db.execute("INSERT INTO gemini_analysis(created_at,context_hash,model,result_json) VALUES(?,?,?,?)",
                            (__import__("time").time(),digest,self.model,json.dumps(result,sort_keys=True)))
            return result
        except Exception as exc:
            error_type=type(exc).__name__
            if "timeout" in error_type.lower():
                self.db.event(
                    "GEMINI_TIMEOUT",
                    "WARNING",
                    {
                        "error":error_type,
                        "timeout_seconds":self.timeout_seconds,
                    },
                )
                return {
                    "status":"TIMEOUT",
                    "effect_bps":0.0,
                    "timeout_seconds":self.timeout_seconds,
                }
            self.db.event("GEMINI_UNAVAILABLE","WARNING",{"error":error_type})
            return {"status":"UNAVAILABLE","effect_bps":0.0}

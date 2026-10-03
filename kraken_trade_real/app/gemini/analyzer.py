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
    def __init__(
        self,
        api_key:str,
        model:str,
        enabled:bool,
        db:Any,
        timeout_seconds:int=30,
        fallback_models:str="",
    )->None:
        self.api_key=api_key
        self.model=model.strip()
        self.enabled=enabled
        self.db=db
        self.timeout_seconds=max(5,min(120,int(timeout_seconds)))
        self.models=self._model_pool(self.model,fallback_models)
        self._client=None
        self.last_model=""

    @staticmethod
    def _model_pool(primary:str,fallback_models:str)->list[str]:
        models=[]
        for candidate in [primary,*str(fallback_models).split(",")]:
            name=str(candidate).strip()
            if name and name not in models:
                models.append(name)
        return models

    @staticmethod
    def _error_category(exc:Exception)->str:
        detail=f"{type(exc).__name__}:{exc}".lower()
        if "timeout" in detail or "deadline exceeded" in detail:
            return "TIMEOUT"
        if any(token in detail for token in (
            "429","resource_exhausted","quota","rate limit","rate_limit","too many requests",
        )):
            return "QUOTA_EXHAUSTED"
        if any(token in detail for token in (
            "404","not found","model not found","unsupported model","invalid model",
        )):
            return "MODEL_UNAVAILABLE"
        if any(token in detail for token in (
            "401","unauthorized","invalid api key","authentication",
        )):
            return "AUTH_ERROR"
        if any(token in detail for token in ("503","unavailable","overloaded","temporarily")):
            return "UNAVAILABLE"
        return "ERROR"

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
        if client is None:return {"status":"DISABLED","effect_bps":0.0,"model":""}
        prompt=("Research only. Never return an order, trade instruction, position size, leverage instruction "
                "or execution command. Return JSON matching this schema. NEWS="+json.dumps(news[:20],default=str)+
                " CONTEXT="+json.dumps(market_context,default=str))
        attempted=[]
        failures=[]
        self.last_model=""
        for model in self.models:
            attempted.append(model)
            self.db.event(
                "GEMINI_MODEL_ATTEMPT",
                "INFO",
                {"model":model,"attempt":len(attempted),"timeout_seconds":self.timeout_seconds},
            )
            try:
                response=client.interactions.create(
                    model=model,input=prompt,
                    response_format={
                        "type":"text",
                        "mime_type":"application/json",
                        "schema":GeminiResult.model_json_schema(),
                    },
                    store=False,
                    timeout=float(self.timeout_seconds),
                )
                parsed=GeminiResult.model_validate_json(response.output_text).model_dump()
                result={
                    "status":"OK",
                    **parsed,
                    "model":model,
                    "attempted_models":attempted,
                    "fallback_used":len(attempted)>1,
                }
                digest=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
                self.db.execute(
                    "INSERT INTO gemini_analysis(created_at,context_hash,model,result_json) VALUES(?,?,?,?)",
                    (__import__("time").time(),digest,model,json.dumps(result,sort_keys=True)),
                )
                self.last_model=model
                if len(attempted)>1:
                    self.db.event(
                        "GEMINI_MODEL_FALLBACK",
                        "WARNING",
                        {
                            "primary_model":self.models[0],
                            "selected_model":model,
                            "attempted_models":attempted,
                        },
                    )
                return result
            except Exception as exc:
                category=self._error_category(exc)
                failure={"model":model,"reason":category,"error":type(exc).__name__}
                failures.append(failure)
                self.db.event("GEMINI_MODEL_FAILED","WARNING",failure)
                if category=="TIMEOUT":
                    self.db.event(
                        "GEMINI_TIMEOUT",
                        "WARNING",
                        {
                            "model":model,
                            "timeout_seconds":self.timeout_seconds,
                            "attempt":len(attempted),
                        },
                    )
                if category=="QUOTA_EXHAUSTED":
                    self.db.event(
                        "GEMINI_QUOTA_EXHAUSTED",
                        "WARNING",
                        {"model":model,"attempt":len(attempted)},
                    )
                if category=="AUTH_ERROR":
                    break

        categories=[item["reason"] for item in failures]
        if failures and all(reason=="TIMEOUT" for reason in categories):
            status="TIMEOUT"
        elif failures and all(reason=="QUOTA_EXHAUSTED" for reason in categories):
            status="QUOTA_EXHAUSTED"
        elif failures and all(reason=="MODEL_UNAVAILABLE" for reason in categories):
            status="MODEL_UNAVAILABLE"
        else:
            status="DEGRADED"
        self.db.event(
            "GEMINI_FALLBACK_EXHAUSTED",
            "WARNING",
            {
                "attempted_models":attempted,
                "failures":failures,
                "status":status,
                "continuation":"ZERO_IMPACT_AND_CONTINUE",
            },
        )
        return {
            "status":status,
            "effect_bps":0.0,
            "expected_impact_bps":0.0,
            "model":"",
            "attempted_models":attempted,
            "fallback_used":len(attempted)>1,
            "failures":failures,
            "reason":"ALL_MODELS_FAILED",
            "timeout_seconds":self.timeout_seconds if status=="TIMEOUT" else 0,
        }

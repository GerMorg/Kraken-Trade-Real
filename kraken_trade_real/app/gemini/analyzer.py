from __future__ import annotations

from pydantic import BaseModel, Field
import hashlib
import json
import signal
import threading
from typing import Any, Callable


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
        self._client_init_timeout_seconds=min(10.0,float(self.timeout_seconds))
        self._client_error=""
        self.last_model=""
        self._model_cooldown_until:dict[str,float]={}
        self._model_cooldown_seconds=900.0

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

    @staticmethod
    def _run_hard_timeout(operation:Callable[[],Any],timeout_seconds:float)->Any:
        if (
            timeout_seconds <= 0
            or not hasattr(signal,"SIGALRM")
            or threading.current_thread() is not threading.main_thread()
        ):
            return operation()
        previous_handler=signal.getsignal(signal.SIGALRM)

        def handler(signum,frame):
            raise TimeoutError(f"operation exceeded hard timeout of {timeout_seconds:.1f}s")

        signal.signal(signal.SIGALRM,handler)
        signal.setitimer(signal.ITIMER_REAL,timeout_seconds)
        try:
            return operation()
        finally:
            signal.setitimer(signal.ITIMER_REAL,0)
            signal.signal(signal.SIGALRM,previous_handler)

    def _build_client(self):
        from google import genai
        from google.genai import types
        return genai.Client(
            api_key=self.api_key,
            http_options=types.HttpOptions(
                timeout=self.timeout_seconds*1000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def _client_for_use(self):
        if not self.enabled or not self.api_key:
            return None
        if self._client is None:
            self._client_error=""
            init_timeout=self._client_init_timeout_seconds
            self.db.event("GEMINI_CLIENT_INIT_START","INFO",{"timeout_seconds":init_timeout})
            try:
                self._client=self._run_hard_timeout(self._build_client,init_timeout)
            except TimeoutError:
                self._client_error="TIMEOUT"
                self.db.event("GEMINI_CLIENT_INIT_TIMEOUT","ERROR",{"timeout_seconds":init_timeout})
                return None
            except Exception as exc:
                self._client_error="ERROR"
                self.db.event(
                    "GEMINI_CLIENT_INIT_FAILED","ERROR",
                    {"error":f"{type(exc).__name__}:{str(exc)[:800]}"},
                )
                return None
            self.db.event("GEMINI_CLIENT_READY","INFO",{})
        return self._client

    def analyze(self,news:list[dict[str,Any]],market_context:dict[str,Any])->dict[str,Any]:
        client=self._client_for_use()
        if client is None:
            if not self.enabled or not self.api_key:
                return {"status":"DISABLED","effect_bps":0.0,"model":""}
            status="TIMEOUT" if self._client_error=="TIMEOUT" else "DEGRADED"
            self.db.event(
                "GEMINI_FALLBACK_EXHAUSTED",
                "WARNING",
                {
                    "attempted_models":[],
                    "failures":[{"stage":"CLIENT_INIT","reason":self._client_error or "ERROR"}],
                    "status":status,
                    "continuation":"ZERO_IMPACT_AND_CONTINUE",
                },
            )
            return {
                "status":status,
                "effect_bps":0.0,
                "expected_impact_bps":0.0,
                "model":"",
                "attempted_models":[],
                "fallback_used":False,
                "reason":"CLIENT_INIT_FAILED",
                "timeout_seconds":self.timeout_seconds if status=="TIMEOUT" else 0,
            }
        prompt=("Research only. Never return an order, trade instruction, position size, leverage instruction "
                "or execution command. Return JSON matching this schema. NEWS="+json.dumps(news[:20],default=str)+
                " CONTEXT="+json.dumps(market_context,default=str))
        attempted=[]
        failures=[]
        self.last_model=""
        now=__import__("time").time()
        for model in self.models:
            cooldown_until=self._model_cooldown_until.get(model,0.0)
            if cooldown_until>now:
                self.db.event("GEMINI_MODEL_SKIPPED_COOLDOWN","INFO",{"model":model,"remaining_seconds":round(cooldown_until-now,1)})
                continue
            attempted.append(model)
            self.db.event(
                "GEMINI_MODEL_ATTEMPT",
                "INFO",
                {"model":model,"attempt":len(attempted),"timeout_seconds":self.timeout_seconds},
            )
            try:
                response=self._run_hard_timeout(
                    lambda: client.interactions.create(
                        model=model,input=prompt,
                        response_format={
                            "type":"text",
                            "mime_type":"application/json",
                            "schema":GeminiResult.model_json_schema(),
                        },
                        store=False,
                        timeout=float(self.timeout_seconds),
                    ),
                    float(self.timeout_seconds),
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
                if category in {"QUOTA_EXHAUSTED","MODEL_UNAVAILABLE"}:
                    self._model_cooldown_until[model]=__import__("time").time()+self._model_cooldown_seconds
                elif category=="TIMEOUT":
                    self._model_cooldown_until[model]=__import__("time").time()+300.0
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

        if not attempted:
            return {"status":"DEGRADED","effect_bps":0.0,"expected_impact_bps":0.0,"model":"","attempted_models":[],"fallback_used":False,"reason":"ALL_MODELS_IN_COOLDOWN","timeout_seconds":0}
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

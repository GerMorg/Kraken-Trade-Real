from __future__ import annotations
import json,threading,time
from typing import Any,Callable
import websocket
class SequenceTracker:
    def __init__(self):self.last=None
    def observe(self,sequence):
        if sequence is None:return True
        ok=self.last is None or sequence==self.last+1;self.last=sequence;return ok
    def reset(self):self.last=None
class WebSocketSupervisor:
    def __init__(self,audit:Any,recovery:Any,token_provider:Callable[[],dict]|None=None):
        self.audit=audit;self.recovery=recovery;self.token_provider=token_provider;self.stop_event=threading.Event();self.thread=None
        self.market_sequence=SequenceTracker();self.private_sequence=SequenceTracker()
    def start(self):
        if self.thread and self.thread.is_alive():return
        self.stop_event.clear();self.thread=threading.Thread(target=self._run,name="kraken-ws",daemon=True);self.thread.start()
    def stop(self):self.stop_event.set()
    def on_message(self,payload:str,private:bool=False):
        try:data=json.loads(payload)
        except json.JSONDecodeError:self.recovery.issue("DATA_STALE","WS_INVALID_JSON");return
        seq=data.get("sequence")
        if not (self.private_sequence if private else self.market_sequence).observe(int(seq) if seq is not None else None):
            self.audit.emit("WS_SEQUENCE_GAP","ERROR",private=private,sequence=seq);self.recovery.issue("SEQUENCE_GAP",f"private={private}")
    def _run(self):
        delay=1.0
        while not self.stop_event.is_set():
            try:
                ws=websocket.create_connection("wss://ws.kraken.com/v2",timeout=10)
                ws.send(json.dumps({"method":"subscribe","params":{"channel":"ticker","symbol":[],"snapshot":True}}))
                self.audit.emit("PUBLIC_WS_CONNECTED")
                while not self.stop_event.is_set():
                    try:msg=ws.recv()
                    except Exception:break
                    if msg:self.on_message(msg)
                ws.close();delay=1
            except Exception as exc:
                self.recovery.issue("KRAKEN_UNAVAILABLE",f"WS:{type(exc).__name__}")
                self.stop_event.wait(delay);delay=min(60.0,delay*2)

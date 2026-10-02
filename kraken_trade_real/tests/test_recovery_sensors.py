from app.recovery import CircuitBreaker, RecoveryManager
from app.sensors import SensorPublisher


def test_circuit_breaker_blocks_new_risk(db):
    breaker=CircuitBreaker()
    breaker.trip("SEQUENCE_GAP","3->7")
    assert breaker.active is True
    RecoveryManager(db,type("A",(),{"emit":lambda *a,**k:None})(),breaker).recovered("SEQUENCE_GAP")
    assert breaker.active is True


def test_sensor_failure_is_non_blocking(monkeypatch):
    class U:
        def __call__(self,*args,**kwargs): raise OSError("down")
    import app.sensors.publisher as module
    monkeypatch.setattr(module,"urlopen",U())
    publisher=SensorPublisher(True,"token")
    publisher.publish(SensorPublisher.states("READY","READY","cycle"))

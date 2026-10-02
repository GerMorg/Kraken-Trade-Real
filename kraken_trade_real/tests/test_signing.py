from app.kraken.signing import sign_futures, sign_spot


def test_spot_signature_is_deterministic():
    data={"nonce":123456,"pair":"XBT/EUR","type":"buy"}
    secret="c2VjcmV0"
    a=sign_spot("/0/private/AddOrder",data,secret)
    assert a==sign_spot("/0/private/AddOrder",data,secret)
    assert len(a)>40


def test_futures_signature_is_deterministic():
    a=sign_futures("/api/v3/sendorder","orderType=lmt&symbol=PF_XBTUSD", "c2VjcmV0")
    assert a==sign_futures("/api/v3/sendorder","orderType=lmt&symbol=PF_XBTUSD", "c2VjcmV0")
    assert len(a)>40

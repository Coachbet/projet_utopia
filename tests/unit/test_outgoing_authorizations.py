"""Un nonce sortant n'autorise qu'une identité, une cible et un appel."""

from vocalguard.application.outgoing import OutgoingAuthorizations


def test_authorization_is_single_use_identity_bound_and_reserves_active_call():
    store = OutgoingAuthorizations()
    grant = store.issue(identity="protected", number="+33612345678")
    assert store.consume(grant.token, identity="intruder", call_id="CA_wrong") is None
    assert store.consume(grant.token, identity="protected", call_id="CA_call") == grant.number
    assert store.consume(grant.token, identity="protected", call_id="CA_call") == grant.number
    assert store.consume("different", identity="protected", call_id="CA_call") is None
    assert store.consume(grant.token, identity="protected", call_id="CA_replay") is None
    assert store.issue(identity="protected", number="+33699999999") is None
    assert store.owns("CA_call")
    store.forget("CA_wrong")
    assert store.busy
    store.forget("CA_call")
    assert not store.busy
    assert store.issue(identity="protected", number="+33699999999") is not None


def test_token_expires_and_new_authorization_invalidates_old_token():
    now = [100.0]
    store = OutgoingAuthorizations(clock=lambda: now[0])
    old = store.issue(identity="protected", number="+33612345678")
    current = store.issue(identity="protected", number="+33699999999")
    assert old.token != current.token
    assert store.consume(old.token, identity="protected", call_id="CA_old") is None
    now[0] += 60
    assert store.consume(current.token, identity="protected", call_id="CA_expired") is None
    assert not store.busy


def test_child_sid_is_bound_once_and_cannot_modify_another_call():
    store = OutgoingAuthorizations()
    grant = store.issue(identity="protected", number="+33612345678")
    store.consume(grant.token, identity="protected", call_id="CA_parent")
    assert not store.bind_child("CA_other", "CA_intruder")
    assert store.bind_child("CA_parent", "CA_child")
    assert store.bind_child("CA_parent", "CA_child")
    assert not store.bind_child("CA_parent", "CA_intruder")

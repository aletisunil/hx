"""PKCE is the only thing standing between a stolen redirect and a token."""

from __future__ import annotations

from hx.auth.oauth.pkce import challenge_for, generate_pkce, random_state


def test_challenge_matches_the_rfc_7636_vector() -> None:
    """Appendix B of RFC 7636. If this drifts, every login breaks at once."""
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert challenge_for(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_generated_pairs_are_self_consistent_and_url_safe() -> None:
    pkce = generate_pkce()
    assert pkce.method == "S256"
    assert challenge_for(pkce.verifier) == pkce.challenge
    # base64url with no padding: anything else is mangled by the query string.
    for value in (pkce.verifier, pkce.challenge):
        assert "=" not in value and "+" not in value and "/" not in value
    assert 43 <= len(pkce.verifier) <= 128, "outside the length the spec allows"


def test_every_verifier_and_state_is_fresh() -> None:
    """A reused verifier lets one intercepted redirect unlock a later login."""
    assert len({generate_pkce().verifier for _ in range(20)}) == 20
    assert len({random_state() for _ in range(20)}) == 20

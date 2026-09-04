"""
The configuration a production deployment is not allowed to boot without.

Each of these guards a failure that is silent when it happens and expensive
afterwards. They are tested against a dict rather than the real environment so
they are honest, fast, and cannot leak a real value into a failure message.
"""

from __future__ import annotations

import pytest

from student.production import (ProductionMisconfigured, check, describe,
                                enforce, is_production)

GOOD = {
    "QUINTEK_ENV": "production",
    "QUINTEK_DATABASE_URL": "postgresql://user:pw@host/db?sslmode=require",
    "QUINTEK_CORS_ORIGIN": "*",
}


def test_development_is_unconstrained():
    """A guard that makes local work harder gets switched off. This one doesn't."""
    assert check({}) == []
    assert check({"QUINTEK_ENV": "development"}) == []
    assert not is_production({})


def test_a_correctly_configured_production_passes():
    assert check(GOOD) == []
    enforce(GOOD)


def test_production_refuses_ephemeral_sqlite():
    """
    The blocker this whole migration exists to close.

    Booting production on a container filesystem loses every learner's data on
    the next redeploy, and looks perfectly healthy until it does.
    """
    problems = check({**GOOD, "QUINTEK_DATABASE_URL": ""})
    assert len(problems) == 1
    assert "ephemeral" in problems[0]


def test_production_refuses_a_database_connection_without_tls():
    problems = check({**GOOD,
                      "QUINTEK_DATABASE_URL": "postgresql://u:pw@h/db?sslmode=disable"})
    assert len(problems) == 1
    assert "TLS" in problems[0]


def test_production_refuses_an_unset_cors_origin():
    """
    Not "refuses `*`" -- refuses an unmade decision.

    `*` is what the Android WebView requires, because its bundle loads from
    file:/// and its Origin is the literal string "null", which cannot be
    allowlisted. It is safe here only because the app authenticates with a
    bearer header rather than cookies. So the control is that a human chose,
    not that the value is narrow.
    """
    problems = check({**GOOD, "QUINTEK_CORS_ORIGIN": ""})
    assert len(problems) == 1
    assert "explicit decision" in problems[0]


def test_star_cors_is_a_legitimate_production_choice():
    assert check({**GOOD, "QUINTEK_CORS_ORIGIN": "*"}) == []


def test_a_named_origin_is_also_accepted():
    assert check({**GOOD, "QUINTEK_CORS_ORIGIN": "https://app.example.com"}) == []


def test_production_refuses_a_development_model_override():
    """
    The safety gate that matters most.

    QUINTEK_DEV_CANDIDATE bypasses promotion and routing entirely. Nothing is
    qualified -- the authoritative state is NO MODEL QUALIFIED / INSUFFICIENT
    EVIDENCE -- so this would put an unevaluated model in front of a learner's
    medical questions.
    """
    problems = check({**GOOD, "QUINTEK_DEV_CANDIDATE": "nvidia:some-model"})
    assert len(problems) == 1
    assert "UNQUALIFIED" in problems[0]


def test_missing_ai_and_gateway_keys_are_not_boot_failures():
    """
    Refusing to generate, and refusing to sell, are correct states.

    Turning them into boot failures would make the honest state unreachable
    and is exactly the kind of "fix" the promotion gate exists to prevent.
    """
    assert check({**GOOD, "NVIDIA_API_KEY": "", "RAZORPAY_KEY_ID": ""}) == []


def test_every_problem_is_reported_at_once():
    """One redeploy per fault is not a diagnostic loop anybody should run."""
    problems = check({"QUINTEK_ENV": "production", "QUINTEK_DEV_CANDIDATE": "x:y"})
    assert len(problems) == 3


def test_the_refusal_names_variables_and_never_their_values():
    """
    A refusal that prints the connection string to explain a TLS problem has
    published the database password to report a smaller leak.
    """
    secret = "postgresql://admin:sup3rs3cret@db.example.com/quintek?sslmode=disable"
    with pytest.raises(ProductionMisconfigured) as caught:
        enforce({**GOOD, "QUINTEK_DATABASE_URL": secret})
    message = str(caught.value)
    assert "QUINTEK_DATABASE_URL" in message
    assert "sup3rs3cret" not in message
    assert "db.example.com" not in message
    assert secret not in message


def test_describe_reports_presence_never_value():
    """The boot banner and /health both use this."""
    facts = describe({**GOOD, "NVIDIA_API_KEY": "nvapi-realkeyvalue",
                      "RAZORPAY_KEY_SECRET": "rzp_secret_value"})
    rendered = repr(facts)
    assert facts["ai_key_configured"] is True
    assert "nvapi-realkeyvalue" not in rendered
    assert "rzp_secret_value" not in rendered
    assert "sup3rs3cret" not in rendered
    assert GOOD["QUINTEK_DATABASE_URL"] not in rendered


def test_describe_names_the_persistence_backend_honestly():
    assert describe(GOOD)["persistence"] == "postgresql"
    assert "ephemeral" in describe({"QUINTEK_ENV": "production"})["persistence"]

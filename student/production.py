"""
What a production deployment must have decided before it is allowed to boot.

WHY REFUSE TO START RATHER THAN WARN
------------------------------------
Every check here guards a failure that is silent in production and expensive
afterwards: learner data on a disk that is wiped on redeploy, an unqualified
model answering medical questions, a database password crossing the network in
the clear. A warning in a log nobody reads is not a control. A process that
does not start is.

The checks only run when `QUINTEK_ENV=production` is set explicitly. Local
development, tests and staging are unaffected and need no configuration --
which is the point: a guard that makes the ordinary case harder gets disabled.

WHAT IS DELIBERATELY *NOT* CHECKED
----------------------------------
`NVIDIA_API_KEY` and the Razorpay keys are optional and stay optional. Absent,
the server starts, generation refuses and checkout refuses. Refusing to serve
what you cannot serve is correct behaviour, and turning it into a boot failure
would make the honest state unreachable.

CORS IS AN EXPLICIT DECISION, NOT A RESTRICTED ONE
--------------------------------------------------
The obvious rule -- "never allow `*` in production" -- would break the product,
and it is worth writing down why rather than discovering it in a release.

The Android app loads its bundle from `file:///android_asset/`, so the browser
sends `Origin: null`. A literal `null` origin cannot be allowlisted reliably,
so `*` is what the WebView actually requires. It is safe HERE specifically
because the app authenticates with a bearer header rather than cookies: `*` is
forbidden by the CORS specification from being combined with credentialed
requests, so it grants no ambient authority.

So the control is that the operator must have CHOSEN a value, not that the
value must be narrow. `*` is a legitimate and correct choice for a
WebView-only deployment; an unset variable silently defaulting to `*` is not a
choice at all, and that is what this refuses.
"""

from __future__ import annotations

import os

#: Set to "production" to turn these checks on. Anything else, including
#: unset, leaves them off.
ENV_VAR = "QUINTEK_ENV"
PRODUCTION = "production"


class ProductionMisconfigured(RuntimeError):
    """
    Boot refused. Carries every problem at once.

    Reporting one failure at a time turns configuring a deployment into a
    sequence of redeploys, each revealing the next fault. The message names
    variables, never their values -- a refusal that prints the database URL to
    solve a TLS problem has published the password to fix a smaller leak.
    """


def is_production(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return (source.get(ENV_VAR) or "").strip().lower() == PRODUCTION


def check(env: dict[str, str] | None = None) -> list[str]:
    """
    Every reason this configuration must not serve production, as text.

    Empty means it may. Pure: takes the environment, returns strings, touches
    nothing -- so it is testable without setting real variables.
    """
    source = dict(os.environ if env is None else env)
    if not is_production(source):
        return []

    problems: list[str] = []

    database_url = (source.get("QUINTEK_DATABASE_URL") or "").strip()
    if not database_url:
        problems.append(
            "QUINTEK_DATABASE_URL is not set, so this would run on SQLite files on the "
            "container's local disk. That disk is ephemeral: every redeploy and every "
            "restart would destroy accounts, notebooks, questions, attempts, progress, "
            "revision schedules and billing records. Set it to a managed PostgreSQL "
            "connection string.")
    elif "sslmode=disable" in database_url:
        problems.append(
            "QUINTEK_DATABASE_URL disables TLS (sslmode=disable). The database password "
            "and every learner's data would cross the network in the clear. Use "
            "sslmode=require, or verify-full where the CA is pinned.")

    if (source.get("QUINTEK_CORS_ORIGIN") or "").strip() == "":
        problems.append(
            "QUINTEK_CORS_ORIGIN is not set. It must be an explicit decision in "
            "production rather than a default. Set it to the browser origin that is "
            "allowed to call this service -- or to '*' if this deployment serves the "
            "Android WebView, whose origin is the literal string 'null' and cannot be "
            "allowlisted. '*' is safe here only because the app authenticates with a "
            "bearer header rather than cookies.")

    if (source.get("QUINTEK_DEV_CANDIDATE") or "").strip():
        problems.append(
            "QUINTEK_DEV_CANDIDATE is set. It bypasses promotion and routing, so an "
            "UNQUALIFIED model would answer learners' medical questions. Nothing is "
            "currently qualified -- the authoritative state is NO MODEL QUALIFIED / "
            "INSUFFICIENT EVIDENCE -- and refusing to generate is the correct "
            "behaviour, not a fault to work around. Unset it.")

    return problems


def enforce(env: dict[str, str] | None = None) -> None:
    """Raise `ProductionMisconfigured` listing every problem, or return."""
    problems = check(env)
    if not problems:
        return
    lines = "\n".join(f"  {index}. {problem}"
                      for index, problem in enumerate(problems, start=1))
    raise ProductionMisconfigured(
        f"refusing to start: {len(problems)} production requirement(s) not met.\n{lines}\n"
        "Set these in the platform's environment configuration. Never in a commit.")


def describe(env: dict[str, str] | None = None) -> dict:
    """
    What this deployment is, for the boot banner and `/health`.

    Reports whether each secret is PRESENT, never what it is. An operator
    needs to know the database is configured; nobody needs the password in a
    log line, a screenshot or a support ticket.
    """
    source = dict(os.environ if env is None else env)
    return {
        "environment": (source.get(ENV_VAR) or "development").strip().lower(),
        "persistence": "postgresql" if (source.get("QUINTEK_DATABASE_URL") or "").strip()
                       else "sqlite (ephemeral on a container filesystem)",
        "database_url_configured": bool((source.get("QUINTEK_DATABASE_URL") or "").strip()),
        "cors_origin": (source.get("QUINTEK_CORS_ORIGIN") or "*").strip(),
        "ai_key_configured": bool((source.get("NVIDIA_API_KEY") or "").strip()),
        "gateway_configured": bool((source.get("RAZORPAY_KEY_ID") or "").strip()),
        "development_override_set": bool((source.get("QUINTEK_DEV_CANDIDATE") or "").strip()),
    }

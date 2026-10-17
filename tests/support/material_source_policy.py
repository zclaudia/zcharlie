"""Synthetic policy approval records for isolated tests only; never deployment evidence."""

from datetime import date, timedelta

from investment_agent.domain.security import ControlledIdentity


def synthetic_approved_source_policy(
    source: str, identity: ControlledIdentity, adapter_version: str = "synthetic-1"
) -> dict[str, object]:
    """Build an explicitly synthetic approval for one identity and adapter."""

    today = date.today()
    return {
        source: {
            "licensing": {
                "status": "approved",
                "company_keys": [identity.company_key],
                "citation": "synthetic:policy/licensing",
            },
            "technical_acceptance": {
                "status": "accepted",
                "company_keys": [identity.company_key],
                "adapter_version": adapter_version,
                "reports": [
                    {"observed_on": (today - timedelta(days=3)).isoformat(), "citation": "synthetic:report/a"},
                    {"observed_on": (today - timedelta(days=2)).isoformat(), "citation": "synthetic:report/b"},
                    {"observed_on": (today - timedelta(days=1)).isoformat(), "citation": "synthetic:report/c"},
                ],
            },
        }
    }

"""Factor library (P1-B1/B2/B3): one definition per factor.

Every screening / candidate / holding rule references factors by name from
the default registry in :mod:`investment_agent.factors.catalog`. Factors read data
only through :class:`investment_agent.factors.snapshot_model.SnapshotView`.
"""

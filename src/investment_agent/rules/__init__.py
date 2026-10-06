"""Rules (P1-C): declarative rule schema, validator, rule store and screen engine.

* :mod:`~investment_agent.rules.schema` - templates, instances, conditions;
  canonical YAML / JSON and content hash;
* :mod:`~investment_agent.rules.validation` - typed, aggregated checks;
* :mod:`~investment_agent.rules.templates` - ``config/rule_templates`` files;
* :mod:`~investment_agent.rules.store` - versioned rule store (migration 016);
* :mod:`~investment_agent.rules.screen` - deterministic weekly screen engine;
* :mod:`~investment_agent.rules.screen_job` - the ``screen_run`` queue job.

Rules only reference factors of :data:`investment_agent.factors.catalog.DEFAULT_REGISTRY`;
no LLM takes part in evaluating them (funnel design 11).
"""

"""Card 06: v2 output schema - plan bindings, step results and executor dispatch.

Covers design §4.4: the v1 company output schemas stay frozen byte-for-byte
(golden strings, so careless subclassing cannot drift the parent contracts);
the v2 subtypes add ``plan_bindings`` / ``step_results`` as plain schema
members; and the executor swaps in the v2 output types only for an enforcing
receipt (``validator_version == VALIDATOR_V1``) - render-only receipts and v1
tasks without a receipt keep the current dispatch exactly unchanged.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select, update

from investment_agent.agent.method_receipt import (
    VALIDATOR_RENDER_ONLY,
    VALIDATOR_V1,
    build_receipt,
)
from investment_agent.agent.research import (
    CompanyResearchOutput,
    CompanyStudyOutput,
    MethodCompanyResearchOutput,
    MethodCompanyStudyOutput,
    MethodPlanBinding,
    MethodPlannedProgress,
    MethodStepResult,
    ResearchHypothesis,
    ResearchOutput,
    ResearchProgress,
)
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import DIMENSIONS
from investment_agent.investor.enums import TaskStatus
from investment_agent.investor.method_packages import LEGACY_BASELINE_DEFINITION
from investment_agent.investor.methods import (
    ActivateGlobal,
    MethodBinding,
    MethodConfig,
    MethodService,
    task_method_receipt,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import ToolAction
from investment_agent.storage.models.investor import TaskMethodBinding
from tests.support.conversations import (
    conversation_db as conversation_db,
)
from tests.support.conversations import (
    message,
    new_conversation,
    submit,
)
from tests.support.publication import fixture
from tests.support.research import progress, response, run

# Golden strings of the exact v1 JSON schemas, captured before the v2 subtypes
# were introduced. Any change to the parent output contracts fails these.
GOLDEN_COMPANY_STUDY_OUTPUT_SCHEMA = "{\"$defs\":{\"DimensionAssessment\":{\"additionalProperties\":false,\"properties\":{\"applicable\":{\"title\":\"Applicable\",\"type\":\"boolean\"},\"dimension\":{\"enum\":[\"business_demand\",\"competition_durability\",\"financial_cash\",\"balance_sheet_allocation\",\"price_expectations\",\"downside_counter_case\",\"portfolio_alternatives\"],\"title\":\"Dimension\",\"type\":\"string\"},\"reason\":{\"anyOf\":[{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},{\"type\":\"null\"}],\"default\":null,\"title\":\"Reason\"},\"refs\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/ObjectRef\"},\"title\":\"Refs\",\"type\":\"array\"},\"status\":{\"$ref\":\"#/$defs/DimensionStatus\"}},\"required\":[\"dimension\",\"status\",\"applicable\"],\"title\":\"DimensionAssessment\",\"type\":\"object\"},\"DimensionStatus\":{\"enum\":[\"evidenced\",\"qualitative\",\"missing\"],\"title\":\"DimensionStatus\",\"type\":\"string\"},\"EntityNode\":{\"additionalProperties\":false,\"properties\":{\"ref\":{\"$ref\":\"#/$defs/ObjectRef\",\"allOf\":[{\"properties\":{\"field_path\":{\"type\":\"null\"},\"id\":{\"minimum\":1,\"type\":\"integer\"},\"kind\":{\"const\":\"listing\"}}}],\"description\":\"Exact authorized listing identity only. Case, task, message and method references cannot be rendered as security identities.\"},\"type\":{\"const\":\"entity_ref\",\"title\":\"Type\",\"type\":\"string\"}},\"required\":[\"type\",\"ref\"],\"title\":\"EntityNode\",\"type\":\"object\"},\"NumericNode\":{\"additionalProperties\":false,\"properties\":{\"display_format\":{\"enum\":[\"decimal\",\"percent\",\"date\",\"code\"],\"title\":\"Display Format\",\"type\":\"string\"},\"ref\":{\"$ref\":\"#/$defs/ObjectRef\",\"description\":\"historical_fact requires saved evidence with field_path=\\\"value\\\". calculation_output requires a saved calculation with field_path=\\\"results.<exact result key>\\\" (plural results). A bare record ref with field_path=null is not a numeric value reference.\"},\"role\":{\"$ref\":\"#/$defs/NumericRole\"},\"type\":{\"const\":\"numeric_claim\",\"title\":\"Type\",\"type\":\"string\"}},\"required\":[\"type\",\"ref\",\"role\",\"display_format\"],\"title\":\"NumericNode\",\"type\":\"object\"},\"NumericRole\":{\"enum\":[\"historical_fact\",\"calculation_output\",\"scenario_assumption\",\"threshold\",\"date\",\"code\"],\"title\":\"NumericRole\",\"type\":\"string\"},\"ObjectRef\":{\"additionalProperties\":false,\"properties\":{\"field_path\":{\"anyOf\":[{\"type\":\"string\"},{\"type\":\"null\"}],\"default\":null,\"title\":\"Field Path\"},\"id\":{\"anyOf\":[{\"format\":\"uuid\",\"type\":\"string\"},{\"minimum\":1,\"type\":\"integer\"}],\"title\":\"Id\"},\"kind\":{\"enum\":[\"mandate\",\"case\",\"episode\",\"plan\",\"thesis\",\"draft\",\"publication_check\",\"research_progress\",\"publication_preparation\",\"evidence\",\"disclosure_original\",\"calculation\",\"recommendation\",\"monitor\",\"review\",\"decision\",\"proposal\",\"instruction\",\"memory\",\"method\",\"conversation\",\"message\",\"task\",\"listing\",\"market_snapshot\",\"financial_snapshot\",\"earnings_forecast\",\"disclosure_schedule\",\"business_segment\",\"candidate\",\"watchlist_item\",\"pipeline_run\",\"research_dataset\",\"portfolio_snapshot\",\"portfolio_valuation\",\"portfolio_plan\",\"portfolio_purposes\",\"peer_cohort_snapshot\",\"peer_annual_snapshot\",\"account_fit\",\"candidate_ranking\",\"supplemental_rule\",\"research_event\",\"rule_application\",\"rule_coverage\",\"research_rule_binding\",\"package\",\"topic_case\",\"topic_revision\",\"topic_condition\",\"topic_check\",\"portfolio_stress\",\"decision_observation\"],\"title\":\"Kind\",\"type\":\"string\"},\"revision\":{\"minimum\":1,\"title\":\"Revision\",\"type\":\"integer\"}},\"required\":[\"kind\",\"id\",\"revision\"],\"title\":\"ObjectRef\",\"type\":\"object\"},\"ResearchHypothesis\":{\"additionalProperties\":false,\"properties\":{\"alternative\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Alternative\",\"type\":\"string\"},\"evidence_priority\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Evidence Priority\",\"type\":\"string\"},\"explanation\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Explanation\",\"type\":\"string\"},\"falsification_condition\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Falsification Condition\",\"type\":\"string\"},\"question\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Question\",\"type\":\"string\"}},\"required\":[\"question\",\"explanation\",\"alternative\",\"falsification_condition\",\"evidence_priority\"],\"title\":\"ResearchHypothesis\",\"type\":\"object\"},\"ResearchProgress\":{\"additionalProperties\":false,\"properties\":{\"answered\":{\"default\":false,\"title\":\"Answered\",\"type\":\"boolean\"},\"assumptions\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Assumptions\",\"type\":\"array\"},\"completed_steps\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Completed Steps\",\"type\":\"array\"},\"completion_criteria\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Completion Criteria\",\"type\":\"array\"},\"content\":{\"default\":[],\"items\":{\"discriminator\":{\"mapping\":{\"entity_ref\":\"#/$defs/EntityNode\",\"numeric_claim\":\"#/$defs/NumericNode\",\"text\":\"#/$defs/TextNode\"},\"propertyName\":\"type\"},\"oneOf\":[{\"$ref\":\"#/$defs/TextNode\"},{\"$ref\":\"#/$defs/EntityNode\"},{\"$ref\":\"#/$defs/NumericNode\"}]},\"title\":\"Content\",\"type\":\"array\"},\"counter_refs\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/ObjectRef\"},\"title\":\"Counter Refs\",\"type\":\"array\"},\"dimensions\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/DimensionAssessment\"},\"title\":\"Dimensions\",\"type\":\"array\"},\"evidence_steps\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Evidence Steps\",\"type\":\"array\"},\"fact_refs\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/ObjectRef\"},\"title\":\"Fact Refs\",\"type\":\"array\"},\"gaps\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Gaps\",\"type\":\"array\"},\"hypotheses\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/ResearchHypothesis\"},\"title\":\"Hypotheses\",\"type\":\"array\"},\"inferences\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Inferences\",\"type\":\"array\"},\"kind\":{\"enum\":[\"explanation\",\"local_check\",\"company_research\",\"comparison\",\"review\",\"portfolio_action\"],\"title\":\"Kind\",\"type\":\"string\"},\"limitations\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Limitations\",\"type\":\"array\"},\"question\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Question\",\"type\":\"string\"},\"stop_conditions\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Stop Conditions\",\"type\":\"array\"},\"stopped_because\":{\"anyOf\":[{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},{\"type\":\"null\"}],\"default\":null,\"title\":\"Stopped Because\"}},\"required\":[\"question\",\"kind\"],\"title\":\"ResearchProgress\",\"type\":\"object\"},\"RuleReviewItem\":{\"additionalProperties\":false,\"description\":\"One per-rule disposition inside the semantic review window.\",\"properties\":{\"application_ref\":{\"$ref\":\"#/$defs/ObjectRef\"},\"basis_refs\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/ObjectRef\"},\"title\":\"Basis Refs\",\"type\":\"array\"},\"disposition\":{\"enum\":[\"no_action_required\",\"resolved_with_closure\",\"limitation_acknowledged\"],\"title\":\"Disposition\",\"type\":\"string\"},\"proposal_hash\":{\"anyOf\":[{\"type\":\"string\"},{\"type\":\"null\"}],\"default\":null,\"title\":\"Proposal Hash\"}},\"required\":[\"application_ref\",\"disposition\"],\"title\":\"RuleReviewItem\",\"type\":\"object\"},\"SemanticAssessment\":{\"additionalProperties\":false,\"properties\":{\"claim_id\":{\"format\":\"uuid\",\"title\":\"Claim Id\",\"type\":\"string\"},\"counter_evidence_relevance\":{\"title\":\"Counter Evidence Relevance\",\"type\":\"string\"},\"fact_relation\":{\"title\":\"Fact Relation\",\"type\":\"string\"},\"issues\":{\"default\":[],\"items\":{\"type\":\"string\"},\"title\":\"Issues\",\"type\":\"array\"},\"materiality_reason\":{\"title\":\"Materiality Reason\",\"type\":\"string\"}},\"required\":[\"claim_id\",\"fact_relation\",\"counter_evidence_relevance\",\"materiality_reason\"],\"title\":\"SemanticAssessment\",\"type\":\"object\"},\"SemanticInput\":{\"additionalProperties\":false,\"properties\":{\"assessments\":{\"items\":{\"$ref\":\"#/$defs/SemanticAssessment\"},\"title\":\"Assessments\",\"type\":\"array\"},\"checked_sources\":{\"items\":{\"$ref\":\"#/$defs/ObjectRef\"},\"title\":\"Checked Sources\",\"type\":\"array\"},\"conclusion\":{\"enum\":[\"pass\",\"revise\"],\"title\":\"Conclusion\",\"type\":\"string\"},\"coverage_manifest_hash\":{\"anyOf\":[{\"type\":\"string\"},{\"type\":\"null\"}],\"default\":null,\"title\":\"Coverage Manifest Hash\"},\"coverage_manifest_ref\":{\"anyOf\":[{\"$ref\":\"#/$defs/ObjectRef\"},{\"type\":\"null\"}],\"default\":null},\"preparation_ref\":{\"$ref\":\"#/$defs/ObjectRef\"},\"rule_reviews\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/RuleReviewItem\"},\"title\":\"Rule Reviews\",\"type\":\"array\"},\"unresolved_questions\":{\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Unresolved Questions\",\"type\":\"array\"}},\"required\":[\"preparation_ref\",\"assessments\",\"checked_sources\",\"unresolved_questions\",\"conclusion\"],\"title\":\"SemanticInput\",\"type\":\"object\"},\"TextNode\":{\"additionalProperties\":false,\"properties\":{\"text\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Text\",\"type\":\"string\"},\"type\":{\"const\":\"text\",\"title\":\"Type\",\"type\":\"string\"}},\"required\":[\"type\",\"text\"],\"title\":\"TextNode\",\"type\":\"object\"}},\"additionalProperties\":false,\"description\":\"A new company study publishes a new view, not a maintain/extend review.\",\"properties\":{\"critical_counter_evidence\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/ObjectRef\"},\"title\":\"Critical Counter Evidence\",\"type\":\"array\"},\"progress\":{\"$ref\":\"#/$defs/ResearchProgress\"},\"semantic_review\":{\"anyOf\":[{\"$ref\":\"#/$defs/SemanticInput\"},{\"type\":\"null\"}],\"default\":null}},\"required\":[\"progress\"],\"title\":\"CompanyStudyOutput\",\"type\":\"object\"}"
GOLDEN_COMPANY_RESEARCH_OUTPUT_SCHEMA = "{\"$defs\":{\"DimensionAssessment\":{\"additionalProperties\":false,\"properties\":{\"applicable\":{\"title\":\"Applicable\",\"type\":\"boolean\"},\"dimension\":{\"enum\":[\"business_demand\",\"competition_durability\",\"financial_cash\",\"balance_sheet_allocation\",\"price_expectations\",\"downside_counter_case\",\"portfolio_alternatives\"],\"title\":\"Dimension\",\"type\":\"string\"},\"reason\":{\"anyOf\":[{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},{\"type\":\"null\"}],\"default\":null,\"title\":\"Reason\"},\"refs\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/ObjectRef\"},\"title\":\"Refs\",\"type\":\"array\"},\"status\":{\"$ref\":\"#/$defs/DimensionStatus\"}},\"required\":[\"dimension\",\"status\",\"applicable\"],\"title\":\"DimensionAssessment\",\"type\":\"object\"},\"DimensionStatus\":{\"enum\":[\"evidenced\",\"qualitative\",\"missing\"],\"title\":\"DimensionStatus\",\"type\":\"string\"},\"EntityNode\":{\"additionalProperties\":false,\"properties\":{\"ref\":{\"$ref\":\"#/$defs/ObjectRef\",\"allOf\":[{\"properties\":{\"field_path\":{\"type\":\"null\"},\"id\":{\"minimum\":1,\"type\":\"integer\"},\"kind\":{\"const\":\"listing\"}}}],\"description\":\"Exact authorized listing identity only. Case, task, message and method references cannot be rendered as security identities.\"},\"type\":{\"const\":\"entity_ref\",\"title\":\"Type\",\"type\":\"string\"}},\"required\":[\"type\",\"ref\"],\"title\":\"EntityNode\",\"type\":\"object\"},\"NumericNode\":{\"additionalProperties\":false,\"properties\":{\"display_format\":{\"enum\":[\"decimal\",\"percent\",\"date\",\"code\"],\"title\":\"Display Format\",\"type\":\"string\"},\"ref\":{\"$ref\":\"#/$defs/ObjectRef\",\"description\":\"historical_fact requires saved evidence with field_path=\\\"value\\\". calculation_output requires a saved calculation with field_path=\\\"results.<exact result key>\\\" (plural results). A bare record ref with field_path=null is not a numeric value reference.\"},\"role\":{\"$ref\":\"#/$defs/NumericRole\"},\"type\":{\"const\":\"numeric_claim\",\"title\":\"Type\",\"type\":\"string\"}},\"required\":[\"type\",\"ref\",\"role\",\"display_format\"],\"title\":\"NumericNode\",\"type\":\"object\"},\"NumericRole\":{\"enum\":[\"historical_fact\",\"calculation_output\",\"scenario_assumption\",\"threshold\",\"date\",\"code\"],\"title\":\"NumericRole\",\"type\":\"string\"},\"ObjectRef\":{\"additionalProperties\":false,\"properties\":{\"field_path\":{\"anyOf\":[{\"type\":\"string\"},{\"type\":\"null\"}],\"default\":null,\"title\":\"Field Path\"},\"id\":{\"anyOf\":[{\"format\":\"uuid\",\"type\":\"string\"},{\"minimum\":1,\"type\":\"integer\"}],\"title\":\"Id\"},\"kind\":{\"enum\":[\"mandate\",\"case\",\"episode\",\"plan\",\"thesis\",\"draft\",\"publication_check\",\"research_progress\",\"publication_preparation\",\"evidence\",\"disclosure_original\",\"calculation\",\"recommendation\",\"monitor\",\"review\",\"decision\",\"proposal\",\"instruction\",\"memory\",\"method\",\"conversation\",\"message\",\"task\",\"listing\",\"market_snapshot\",\"financial_snapshot\",\"earnings_forecast\",\"disclosure_schedule\",\"business_segment\",\"candidate\",\"watchlist_item\",\"pipeline_run\",\"research_dataset\",\"portfolio_snapshot\",\"portfolio_valuation\",\"portfolio_plan\",\"portfolio_purposes\",\"peer_cohort_snapshot\",\"peer_annual_snapshot\",\"account_fit\",\"candidate_ranking\",\"supplemental_rule\",\"research_event\",\"rule_application\",\"rule_coverage\",\"research_rule_binding\",\"package\",\"topic_case\",\"topic_revision\",\"topic_condition\",\"topic_check\",\"portfolio_stress\",\"decision_observation\"],\"title\":\"Kind\",\"type\":\"string\"},\"revision\":{\"minimum\":1,\"title\":\"Revision\",\"type\":\"integer\"}},\"required\":[\"kind\",\"id\",\"revision\"],\"title\":\"ObjectRef\",\"type\":\"object\"},\"ResearchHypothesis\":{\"additionalProperties\":false,\"properties\":{\"alternative\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Alternative\",\"type\":\"string\"},\"evidence_priority\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Evidence Priority\",\"type\":\"string\"},\"explanation\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Explanation\",\"type\":\"string\"},\"falsification_condition\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Falsification Condition\",\"type\":\"string\"},\"question\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Question\",\"type\":\"string\"}},\"required\":[\"question\",\"explanation\",\"alternative\",\"falsification_condition\",\"evidence_priority\"],\"title\":\"ResearchHypothesis\",\"type\":\"object\"},\"ResearchProgress\":{\"additionalProperties\":false,\"properties\":{\"answered\":{\"default\":false,\"title\":\"Answered\",\"type\":\"boolean\"},\"assumptions\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Assumptions\",\"type\":\"array\"},\"completed_steps\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Completed Steps\",\"type\":\"array\"},\"completion_criteria\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Completion Criteria\",\"type\":\"array\"},\"content\":{\"default\":[],\"items\":{\"discriminator\":{\"mapping\":{\"entity_ref\":\"#/$defs/EntityNode\",\"numeric_claim\":\"#/$defs/NumericNode\",\"text\":\"#/$defs/TextNode\"},\"propertyName\":\"type\"},\"oneOf\":[{\"$ref\":\"#/$defs/TextNode\"},{\"$ref\":\"#/$defs/EntityNode\"},{\"$ref\":\"#/$defs/NumericNode\"}]},\"title\":\"Content\",\"type\":\"array\"},\"counter_refs\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/ObjectRef\"},\"title\":\"Counter Refs\",\"type\":\"array\"},\"dimensions\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/DimensionAssessment\"},\"title\":\"Dimensions\",\"type\":\"array\"},\"evidence_steps\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Evidence Steps\",\"type\":\"array\"},\"fact_refs\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/ObjectRef\"},\"title\":\"Fact Refs\",\"type\":\"array\"},\"gaps\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Gaps\",\"type\":\"array\"},\"hypotheses\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/ResearchHypothesis\"},\"title\":\"Hypotheses\",\"type\":\"array\"},\"inferences\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Inferences\",\"type\":\"array\"},\"kind\":{\"enum\":[\"explanation\",\"local_check\",\"company_research\",\"comparison\",\"review\",\"portfolio_action\"],\"title\":\"Kind\",\"type\":\"string\"},\"limitations\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Limitations\",\"type\":\"array\"},\"question\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Question\",\"type\":\"string\"},\"stop_conditions\":{\"default\":[],\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Stop Conditions\",\"type\":\"array\"},\"stopped_because\":{\"anyOf\":[{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},{\"type\":\"null\"}],\"default\":null,\"title\":\"Stopped Because\"}},\"required\":[\"question\",\"kind\"],\"title\":\"ResearchProgress\",\"type\":\"object\"},\"ReviewEvent\":{\"additionalProperties\":false,\"properties\":{\"actor\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Actor\",\"type\":\"string\"},\"completed_at\":{\"format\":\"date-time\",\"title\":\"Completed At\",\"type\":\"string\"},\"created_at\":{\"format\":\"date-time\",\"title\":\"Created At\",\"type\":\"string\"},\"due_at\":{\"format\":\"date-time\",\"title\":\"Due At\",\"type\":\"string\"},\"evidence_diff_refs\":{\"items\":{\"$ref\":\"#/$defs/ObjectRef\"},\"title\":\"Evidence Diff Refs\",\"type\":\"array\"},\"id\":{\"format\":\"uuid\",\"title\":\"Id\",\"type\":\"string\"},\"new_recommendation_ref\":{\"anyOf\":[{\"$ref\":\"#/$defs/ObjectRef\"},{\"type\":\"null\"}]},\"origin_ref\":{\"anyOf\":[{\"$ref\":\"#/$defs/ObjectRef\"},{\"type\":\"null\"}]},\"rationale\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Rationale\",\"type\":\"string\"},\"reasoning_revision\":{\"default\":false,\"title\":\"Reasoning Revision\",\"type\":\"boolean\"},\"recommendation_ref\":{\"$ref\":\"#/$defs/ObjectRef\"},\"result\":{\"enum\":[\"maintain\",\"revise\",\"withdraw\",\"inconclusive\"],\"title\":\"Result\",\"type\":\"string\"},\"revision\":{\"minimum\":1,\"title\":\"Revision\",\"type\":\"integer\"},\"schema_version\":{\"const\":1,\"default\":1,\"title\":\"Schema Version\",\"type\":\"integer\"},\"started_at\":{\"format\":\"date-time\",\"title\":\"Started At\",\"type\":\"string\"},\"trigger_refs\":{\"items\":{\"$ref\":\"#/$defs/ObjectRef\"},\"title\":\"Trigger Refs\",\"type\":\"array\"}},\"required\":[\"id\",\"revision\",\"created_at\",\"actor\",\"origin_ref\",\"trigger_refs\",\"recommendation_ref\",\"evidence_diff_refs\",\"result\",\"new_recommendation_ref\",\"rationale\",\"due_at\",\"started_at\",\"completed_at\"],\"title\":\"ReviewEvent\",\"type\":\"object\"},\"RuleReviewItem\":{\"additionalProperties\":false,\"description\":\"One per-rule disposition inside the semantic review window.\",\"properties\":{\"application_ref\":{\"$ref\":\"#/$defs/ObjectRef\"},\"basis_refs\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/ObjectRef\"},\"title\":\"Basis Refs\",\"type\":\"array\"},\"disposition\":{\"enum\":[\"no_action_required\",\"resolved_with_closure\",\"limitation_acknowledged\"],\"title\":\"Disposition\",\"type\":\"string\"},\"proposal_hash\":{\"anyOf\":[{\"type\":\"string\"},{\"type\":\"null\"}],\"default\":null,\"title\":\"Proposal Hash\"}},\"required\":[\"application_ref\",\"disposition\"],\"title\":\"RuleReviewItem\",\"type\":\"object\"},\"SemanticAssessment\":{\"additionalProperties\":false,\"properties\":{\"claim_id\":{\"format\":\"uuid\",\"title\":\"Claim Id\",\"type\":\"string\"},\"counter_evidence_relevance\":{\"title\":\"Counter Evidence Relevance\",\"type\":\"string\"},\"fact_relation\":{\"title\":\"Fact Relation\",\"type\":\"string\"},\"issues\":{\"default\":[],\"items\":{\"type\":\"string\"},\"title\":\"Issues\",\"type\":\"array\"},\"materiality_reason\":{\"title\":\"Materiality Reason\",\"type\":\"string\"}},\"required\":[\"claim_id\",\"fact_relation\",\"counter_evidence_relevance\",\"materiality_reason\"],\"title\":\"SemanticAssessment\",\"type\":\"object\"},\"SemanticInput\":{\"additionalProperties\":false,\"properties\":{\"assessments\":{\"items\":{\"$ref\":\"#/$defs/SemanticAssessment\"},\"title\":\"Assessments\",\"type\":\"array\"},\"checked_sources\":{\"items\":{\"$ref\":\"#/$defs/ObjectRef\"},\"title\":\"Checked Sources\",\"type\":\"array\"},\"conclusion\":{\"enum\":[\"pass\",\"revise\"],\"title\":\"Conclusion\",\"type\":\"string\"},\"coverage_manifest_hash\":{\"anyOf\":[{\"type\":\"string\"},{\"type\":\"null\"}],\"default\":null,\"title\":\"Coverage Manifest Hash\"},\"coverage_manifest_ref\":{\"anyOf\":[{\"$ref\":\"#/$defs/ObjectRef\"},{\"type\":\"null\"}],\"default\":null},\"preparation_ref\":{\"$ref\":\"#/$defs/ObjectRef\"},\"rule_reviews\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/RuleReviewItem\"},\"title\":\"Rule Reviews\",\"type\":\"array\"},\"unresolved_questions\":{\"items\":{\"maxLength\":20000,\"minLength\":1,\"type\":\"string\"},\"title\":\"Unresolved Questions\",\"type\":\"array\"}},\"required\":[\"preparation_ref\",\"assessments\",\"checked_sources\",\"unresolved_questions\",\"conclusion\"],\"title\":\"SemanticInput\",\"type\":\"object\"},\"TextNode\":{\"additionalProperties\":false,\"properties\":{\"text\":{\"maxLength\":20000,\"minLength\":1,\"title\":\"Text\",\"type\":\"string\"},\"type\":{\"const\":\"text\",\"title\":\"Type\",\"type\":\"string\"}},\"required\":[\"type\",\"text\"],\"title\":\"TextNode\",\"type\":\"object\"}},\"additionalProperties\":false,\"description\":\"Company runs do not need portfolio or user-control schemas in every request.\\n\\nRuntime validation/authorization still uses the complete ResearchOutput contract.\\nExplicit control conversations and portfolio tasks retain their full schema.\",\"properties\":{\"critical_counter_evidence\":{\"default\":[],\"items\":{\"$ref\":\"#/$defs/ObjectRef\"},\"title\":\"Critical Counter Evidence\",\"type\":\"array\"},\"extend\":{\"default\":false,\"title\":\"Extend\",\"type\":\"boolean\"},\"progress\":{\"$ref\":\"#/$defs/ResearchProgress\"},\"review\":{\"anyOf\":[{\"$ref\":\"#/$defs/ReviewEvent\"},{\"type\":\"null\"}],\"default\":null},\"semantic_review\":{\"anyOf\":[{\"$ref\":\"#/$defs/SemanticInput\"},{\"type\":\"null\"}],\"default\":null}},\"required\":[\"progress\"],\"title\":\"CompanyResearchOutput\",\"type\":\"object\"}"


def _golden_schema(output_type: type) -> str:
    return json.dumps(output_type.model_json_schema(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_v1_company_study_output_schema_stays_frozen() -> None:
    assert _golden_schema(CompanyStudyOutput) == GOLDEN_COMPANY_STUDY_OUTPUT_SCHEMA


def test_v1_company_research_output_schema_stays_frozen() -> None:
    assert _golden_schema(CompanyResearchOutput) == GOLDEN_COMPANY_RESEARCH_OUTPUT_SCHEMA


# --------------------------------------------------------------------------- #
# v2 subtypes: only additional fields, parent schemas untouched
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("output_type", [MethodCompanyStudyOutput, MethodCompanyResearchOutput])
def test_v2_subtypes_only_add_plan_and_step_result_fields(output_type: type) -> None:
    parent = CompanyStudyOutput if output_type is MethodCompanyStudyOutput else CompanyResearchOutput
    v2 = output_type.model_json_schema()
    v1 = parent.model_json_schema()
    # The v2 progress schema repeats every inherited field with the identical
    # definition and adds exactly the two method fields.
    planned = v2["$defs"]["MethodPlannedProgress"]
    v1_progress = v1["$defs"]["ResearchProgress"]["properties"]
    assert set(planned["properties"]) == set(v1_progress) | {"plan_bindings", "step_results"}
    for name, prop in v1_progress.items():
        assert planned["properties"][name] == prop
    assert planned["additionalProperties"] is False and planned["required"] == v1["$defs"]["ResearchProgress"]["required"]
    assert planned["properties"]["plan_bindings"]["items"] == {"$ref": "#/$defs/MethodPlanBinding"}
    assert planned["properties"]["step_results"]["items"] == {"$ref": "#/$defs/MethodStepResult"}
    binding = v2["$defs"]["MethodPlanBinding"]
    assert binding["properties"]["evidence_step_index"] == {"minimum": 0, "title": "Evidence Step Index", "type": "integer"}
    result = v2["$defs"]["MethodStepResult"]
    assert result["properties"]["status"] == {"enum": ["satisfied", "completed_with_gap"], "title": "Status", "type": "string"}


# --------------------------------------------------------------------------- #
# Serialization contract: v1 JSON unchanged, v2 payload closed on the v1 type
# --------------------------------------------------------------------------- #


def _company_payload() -> dict[str, Any]:
    """A complete v1 company_research progress payload with no method fields."""
    return {
        "kind": "company_research",
        "question": "Is the reported cash durable?",
        "hypotheses": [{
            "question": "Is cash durable?",
            "explanation": "Operating cash may recur",
            "alternative": "Temporary working capital release",
            "falsification_condition": "Cash reverses without operating growth",
            "evidence_priority": "Check cash components first",
        }],
        "evidence_steps": ["Check reported cash and opposing explanations"],
        "completion_criteria": ["Explain scope and material gaps"],
        "stop_conditions": ["Stop when critical source unavailable"],
        "dimensions": [
            {"dimension": name, "status": "missing", "applicable": False, "reason": "No company material was read."}
            for name in DIMENSIONS
        ],
        "completed_steps": ["Clarified scope and alternatives"],
        "answered": False,
        "stopped_because": "The available material cannot answer the business thesis.",
    }


def test_v1_json_deserializes_into_v1_types_unchanged() -> None:
    payload = _company_payload()
    progress = ResearchProgress.model_validate(payload)
    dumped = progress.model_dump(mode="json")
    assert "plan_bindings" not in dumped and "step_results" not in dumped  # no method fields injected
    assert progress.kind == "company_research" and [d.dimension for d in progress.dimensions] == list(DIMENSIONS)
    output = CompanyStudyOutput.model_validate({"progress": payload})
    assert output.progress == progress
    # v1 JSON also loads into the v2 output subtypes: extra fields default empty.
    planned = MethodCompanyStudyOutput.model_validate({"progress": payload}).progress
    assert planned.plan_bindings == () and planned.step_results == ()


def test_v2_progress_round_trips_bindings_and_results() -> None:
    payload = {
        **_company_payload(),
        "plan_bindings": [{"evidence_step_index": 0, "method_step_id": "falsifiable_plan"}],
        "step_results": [
            {"method_step_id": "falsifiable_plan", "evidence_step_indices": [0], "status": "satisfied"}
        ],
    }
    with pytest.raises(ValidationError):
        ResearchProgress.model_validate(payload)  # extra=forbid keeps the v1 contract closed
    progress = MethodPlannedProgress.model_validate(payload)
    assert isinstance(progress, ResearchProgress)  # existing validators receive v2 instances unchanged
    dumped = progress.model_dump(mode="json")
    assert dumped["plan_bindings"] == payload["plan_bindings"]
    for dumped_result, payload_result in zip(dumped["step_results"], payload["step_results"], strict=True):
        assert all(dumped_result[key] == value for key, value in payload_result.items())
    result = MethodStepResult(method_step_id="counter_analysis", status="completed_with_gap",
        action_ids=(uuid4(),), hypothesis_indices=(0,), gap_reason="Counter source stayed unavailable.")
    assert result.model_dump(mode="json")["action_ids"] == [str(result.action_ids[0])]
    with pytest.raises(ValidationError):
        MethodPlanBinding(evidence_step_index=-1, method_step_id="falsifiable_plan")
    with pytest.raises(ValidationError):
        MethodStepResult(method_step_id="counter_analysis", status="unknown")


# --------------------------------------------------------------------------- #
# Executor dispatch through the real worker (tests/test_agent_research.py pattern)
# --------------------------------------------------------------------------- #


async def _saved_baseline() -> dict[str, Any]:
    async with session_scope() as session:
        return await MethodService(session).save_package(LEGACY_BASELINE_DEFINITION, source="output-schema-tests")


async def _activate(saved: dict[str, Any]) -> None:
    async with session_scope() as session:
        await MethodService(session).activate_global(
            ActivateGlobal(
                mutation_id=uuid4(),
                package_ref=ObjectRef(kind="package", id=UUID(saved["artifact_id"]), revision=saved["version"]),
                expected_revision=0,
                reason="Enable the reviewed baseline package",
            ),
            actor="authenticated_user",
        )


async def _hand_frozen_receipt(
    task_id: UUID, subject: ObjectRef, saved: dict[str, Any], validator_version: str
) -> None:
    """Freeze a receipt the admission path no longer produces (card 07 froze enforcing).

    Render-only receipts are historical in-flight shapes since card 07; the
    hand-written payload reuses the exact production shapes (frozen binding +
    rendered + hashed method) and enters storage through a Core update: the
    append-only guard is a mapper event, and only the frozen payload of this
    one test task changes - no business path.
    """
    binding = MethodBinding(
        schema_version=2,
        method_ref=ObjectRef(kind="method", id=uuid4(), revision=1),
        config=MethodConfig.model_validate(saved["discipline"]),
        listing_ref=subject,
        package_ref=ObjectRef(kind="package", id=UUID(saved["artifact_id"]), revision=saved["version"]),
        package_hash=saved["content_hash"],
    )
    receipt = build_receipt(binding, LEGACY_BASELINE_DEFINITION, validator_version=validator_version)
    async with session_scope() as session:
        await session.execute(
            update(TaskMethodBinding)
            .where(TaskMethodBinding.task_id == str(task_id))
            .values(payload={"schema_version": 2, "receipt": receipt.model_dump(mode="json")})
        )


async def _hand_frozen_enforcing_receipt(task_id: UUID, subject: ObjectRef, saved: dict[str, Any]) -> None:
    await _hand_frozen_receipt(task_id, subject, saved, VALIDATOR_V1)


def _capture_model(seen: list[Any], final: ResearchOutput):
    def model(messages: Any, info: Any):
        seen.append(info.model_request_parameters.output_object)
        return response(final)

    return model


def _plan_output(dimensions: tuple, kind: str) -> ResearchOutput:
    """A complete plan-style company progress (answered=False) that never publishes."""
    return ResearchOutput(progress=progress(kind=kind, hypotheses=(ResearchHypothesis(
        question="Is cash durable?", explanation="Operating cash may recur",
        alternative="Temporary working capital release",
        falsification_condition="Cash reverses without operating growth",
        evidence_priority="Check cash components first"),),
        evidence_steps=("Check reported cash and opposing explanations",),
        completion_criteria=("Explain scope and material gaps",),
        stop_conditions=("Stop when critical source unavailable",),
        dimensions=dimensions, completed_steps=("Clarified scope and alternatives",),
        answered=False, stopped_because="The frozen method plan still needs evidence before any conclusion."))


def _planned_output(dimensions: tuple, kind: str) -> tuple[ResearchOutput, ResearchOutput]:
    """A first plan plus its final settlement under the baseline package.

    Both blocking steps map onto the single evidence step; the settlement adds
    the step results the §6.2 validator requires (hypothesis indices for the
    plan step; the seven dimension outputs are `missing` by default and so
    already met). The method fields must enter through ``MethodPlannedProgress``
    validation - ``model_copy`` on the v1 class would not serialize them.
    """
    plain = _plan_output(dimensions, kind).progress.model_dump(mode="json")
    plan = MethodPlannedProgress.model_validate({
        **plain,
        "plan_bindings": ({"evidence_step_index": 0, "method_step_id": "falsifiable_plan"},
                          {"evidence_step_index": 0, "method_step_id": "dimension_assessment"}),
    })
    final = plan.model_copy(update={"step_results": (
        MethodStepResult(method_step_id="falsifiable_plan", evidence_step_indices=(0,), status="satisfied",
            hypothesis_indices=(0,)),
        MethodStepResult(method_step_id="dimension_assessment", evidence_step_indices=(0,), status="satisfied"))})
    # The wire dump must carry the method fields: serialize the final through
    # the v2 output type (the plain ResearchOutput field schema would drop the
    # subclass fields the enforcing validator reads).
    output_type = MethodCompanyStudyOutput if kind == "company_research" else MethodCompanyResearchOutput
    return ResearchOutput(progress=plan), output_type(progress=final)


@pytest.mark.asyncio
@pytest.mark.parametrize("research_kind", ["company_research", "review"])
async def test_enforcing_receipt_swaps_to_v2_output_types(conversation_db, research_kind) -> None:
    candidate, _, subject = await fixture()
    saved = await _saved_baseline()
    receipt = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind=research_kind))
    await _hand_frozen_enforcing_receipt(receipt.task.id, subject, saved)
    plan, final = _planned_output(candidate.thesis.assessed_dimensions, research_kind)
    seen: list[Any] = []
    rounds = 0

    def model(messages: Any, info: Any):
        nonlocal rounds
        rounds += 1
        seen.append(info.model_request_parameters.output_object)
        if rounds == 1:
            # The first committed save is the plan baseline: it carries the
            # plan bindings through the swapped v2 tool schema.
            return ModelResponse([ToolCallPart("save_research_progress", {"business": plan.progress.model_dump(mode="json")}, tool_call_id="plan")], usage=RequestUsage(input_tokens=10, output_tokens=5))
        return response(final)

    task = await run(receipt, FunctionModel(model))
    assert task.status == TaskStatus.PARTIAL  # plan-style output never publishes
    assert rounds == 2
    assert len(seen) == 2 and seen[0] is not None
    assert seen[0].name == ("MethodCompanyStudyOutput" if research_kind == "company_research" else "MethodCompanyResearchOutput")
    schema_text = json.dumps(seen[0].json_schema)
    assert "plan_bindings" in schema_text and "step_results" in schema_text
    async with session_scope() as session:
        actions = (await session.scalars(
            select(ToolAction).where(ToolAction.task_id == str(task.id), ToolAction.state == "committed"))).all()
        saved_plan = next(a for a in actions if a.parameters.get("tool") == "save_research_progress")
        assert saved_plan.result["progress"]["plan_bindings"][0]["method_step_id"] == "falsifiable_plan"


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["v1", "render-only"])
@pytest.mark.parametrize("research_kind", ["company_research", "review"])
async def test_v1_and_render_only_receipts_keep_current_dispatch(conversation_db, research_kind, scenario) -> None:
    candidate, _, subject = await fixture()
    if scenario == "render-only":
        saved = await _saved_baseline()
        await _activate(saved)
    receipt = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind=research_kind))
    if scenario == "render-only":
        # Admission freezes enforcing receipts since card 07; rewrite this one
        # test task to the historical in-flight render-only shape.
        await _hand_frozen_receipt(receipt.task.id, subject, saved, VALIDATOR_RENDER_ONLY)
        async with session_scope() as session:
            frozen = await task_method_receipt(session, receipt.task.id)
        assert frozen is not None and frozen.validator_version == VALIDATOR_RENDER_ONLY
    seen: list[Any] = []
    task = await run(receipt, FunctionModel(_capture_model(seen, _plan_output(candidate.thesis.assessed_dimensions, research_kind))))
    assert task.status == TaskStatus.PARTIAL
    assert len(seen) == 1 and seen[0] is not None
    assert seen[0].name == ("CompanyStudyOutput" if research_kind == "company_research" else "CompanyResearchOutput")
    assert "plan_bindings" not in json.dumps(seen[0].json_schema)

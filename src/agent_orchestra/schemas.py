"""Strict canonical schemas for vendor-neutral workflow messages."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class SchemaValidationError(RuntimeError):
    """Raised when an agent returns an invalid canonical schema document."""


INVALID_REVIEW_FIELDS = 'review result has missing or unknown fields'
INVALID_REVIEW_VERDICT = 'review result has an invalid verdict'
INVALID_REVIEW_SUMMARY = 'review result summary must be text'
INVALID_REVIEW_LISTS = 'review result list fields are invalid'
INVALID_REVIEW_FINDINGS = 'review result findings are invalid'
APPROVED_WITH_FINDINGS = 'approved review result cannot contain findings'
CHANGES_REQUESTED_WITHOUT_FINDINGS = (
    'changes_requested review result must contain at least one finding'
)
DUPLICATE_REVIEW_FINDING_IDS = 'review result finding IDs must be unique'
TIMESTAMP_NOT_UTC = 'timestamp must use UTC'


class StrictSchema(BaseModel):
    """Base configuration shared by canonical strict JSON schemas."""

    model_config = ConfigDict(extra='forbid', strict=True)


class InvocationIdentityRecordSchema(StrictSchema):
    """Persist the selected identity for one execution role."""

    vendor: str
    model: str | None
    runtime: str


class ExecutionRoleSchema(StrictSchema):
    """Persist one role's executable configuration."""

    command: list[str]
    identity: InvocationIdentityRecordSchema
    timeout_seconds: int = Field(gt=0)


class ExecutionRecordSchema(StrictSchema):
    """Strict execution context required to resume a run safely."""

    schema_version: Literal[2]
    run_id: str
    objective: str = Field(min_length=1)
    reviewer: ExecutionRoleSchema
    developer: ExecutionRoleSchema
    max_review_iterations: int = Field(gt=0)
    created_at: str

    @field_validator('created_at')
    @classmethod
    def validate_utc_timestamp(cls, value: str) -> str:
        """Require an offset-aware UTC ISO timestamp."""

        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(
            timestamp
        ):
            raise ValueError(TIMESTAMP_NOT_UTC)
        return value


class ReviewFindingSchema(StrictSchema):
    """Canonical structured finding returned by a reviewer."""

    finding_id: str
    severity: Literal['critical', 'high', 'medium', 'low']
    title: str
    path: str | None
    line: int | None = Field(ge=1)
    explanation: str
    acceptance_criterion: str


class ReviewResultSchema(StrictSchema):
    """Canonical result shared by every reviewer runtime adapter."""

    verdict: Literal['approved', 'changes_requested', 'blocked']
    summary: str
    findings: list[ReviewFindingSchema]
    validation: list[str]
    verification_gaps: list[str]

    @model_validator(mode='after')
    def validate_verdict_and_findings(self) -> ReviewResultSchema:
        """Enforce verdict consistency and unique finding identifiers."""

        if self.verdict == 'approved' and self.findings:
            raise ValueError(APPROVED_WITH_FINDINGS)
        if self.verdict == 'changes_requested' and not self.findings:
            raise ValueError(CHANGES_REQUESTED_WITHOUT_FINDINGS)
        finding_ids = [finding.finding_id for finding in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError(DUPLICATE_REVIEW_FINDING_IDS)
        return self


class ValidationOutcomeSchema(StrictSchema):
    """One local validation command and its reported outcome."""

    command: str
    outcome: Literal['passed', 'failed', 'skipped']


class FindingDispositionSchema(StrictSchema):
    """A developer's disposition of one stable reviewer finding."""

    finding_id: str
    disposition: Literal['addressed', 'rejected', 'blocked']
    rationale: str


class DeveloperResultSchema(StrictSchema):
    """Canonical result shared by every developer runtime adapter."""

    status: Literal['ready_for_review', 'blocked', 'failed']
    summary: str
    files_changed: list[str]
    validation: list[ValidationOutcomeSchema]
    dispositions: list[FindingDispositionSchema]
    remaining_risks: list[str]


class DiffScopeSchema(StrictSchema):
    """Immutable Git and worktree scope shared by workflow messages."""

    worktree_path: str
    base_sha: str
    head_sha: str
    diff_digest: str


class ReviewRequestPayloadSchema(StrictSchema):
    """Canonical payload for a review request."""

    objective: str
    allowed_actions: list[str]
    timeout_seconds: int = Field(gt=0)
    artifact_path: str
    prior_review_path: str | None


class ReviewResultPayloadSchema(ReviewResultSchema):
    """Canonical persisted review result payload."""

    artifact_path: str


class MessageIdentitySchema(StrictSchema):
    """Fields common to canonical durable workflow messages."""

    schema_version: Literal[1]
    message_id: str
    in_reply_to: str | None
    run_id: str
    sequence: int = Field(gt=0)
    iteration: int = Field(gt=0)
    created_at: str
    scope: DiffScopeSchema

    @field_validator('message_id')
    @classmethod
    def validate_message_id(cls, value: str) -> str:
        """Require a UUID message identifier without coercing its representation."""

        UUID(value)
        return value

    @field_validator('created_at')
    @classmethod
    def validate_utc_timestamp(cls, value: str) -> str:
        """Require an offset-aware UTC ISO timestamp."""

        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(
            timestamp
        ):
            raise ValueError(TIMESTAMP_NOT_UTC)
        return value


class ReviewRequestMessageSchema(MessageIdentitySchema):
    """Complete canonical review request envelope."""

    in_reply_to: str | None
    message_type: Literal['review_request']
    sender: Literal['orchestrator']
    recipient: Literal['reviewer']
    payload: ReviewRequestPayloadSchema


class ReviewResultMessageSchema(MessageIdentitySchema):
    """Complete canonical review result envelope."""

    in_reply_to: str
    message_type: Literal['review_result']
    sender: Literal['reviewer']
    recipient: Literal['orchestrator']
    payload: ReviewResultPayloadSchema


class RemediationRequestPayloadSchema(StrictSchema):
    """Canonical payload referencing an accepted review without summarizing it."""

    objective: str
    allowed_actions: list[str]
    timeout_seconds: int = Field(gt=0)
    review_result_path: str
    review_artifact_path: str


class RemediationRequestMessageSchema(MessageIdentitySchema):
    """Complete canonical remediation request envelope."""

    in_reply_to: str
    message_type: Literal['remediation_request']
    sender: Literal['orchestrator']
    recipient: Literal['developer']
    payload: RemediationRequestPayloadSchema


class DeveloperHandoffMessageSchema(MessageIdentitySchema):
    """Complete canonical developer handoff envelope."""

    in_reply_to: str
    message_type: Literal['developer_handoff']
    sender: Literal['developer']
    recipient: Literal['orchestrator']
    payload: DeveloperResultSchema


REVIEW_RESULT_SCHEMA = ReviewResultSchema.model_json_schema()
REVIEW_RESULT_SCHEMA['$defs']['ReviewFindingSchema']['required'].sort()
DEVELOPER_RESULT_SCHEMA = DeveloperResultSchema.model_json_schema()


def _review_error_message(error: ValidationError) -> str:
    """Translate Pydantic details into the stable workflow diagnostic contract."""

    details = error.errors()
    message = INVALID_REVIEW_FIELDS
    if any(APPROVED_WITH_FINDINGS in str(detail['msg']) for detail in details):
        message = APPROVED_WITH_FINDINGS
    elif any(
        CHANGES_REQUESTED_WITHOUT_FINDINGS in str(detail['msg']) for detail in details
    ):
        message = CHANGES_REQUESTED_WITHOUT_FINDINGS
    elif any(DUPLICATE_REVIEW_FINDING_IDS in str(detail['msg']) for detail in details):
        message = DUPLICATE_REVIEW_FINDING_IDS
    elif any(
        detail['type'] in {'missing', 'extra_forbidden'} and len(detail['loc']) <= 1
        for detail in details
    ):
        message = INVALID_REVIEW_FIELDS
    else:
        locations = [detail['loc'] for detail in details]
        if any(not location or location[0] == 'findings' for location in locations):
            message = INVALID_REVIEW_FINDINGS
        elif any(location[0] == 'verdict' for location in locations):
            message = INVALID_REVIEW_VERDICT
        elif any(location[0] == 'summary' for location in locations):
            message = INVALID_REVIEW_SUMMARY
        elif any(
            location[0] in {'validation', 'verification_gaps'} for location in locations
        ):
            message = INVALID_REVIEW_LISTS
    return message


def validate_review_result(result: dict[str, Any]) -> None:
    """Validate a canonical reviewer result independently of its runtime."""

    try:
        ReviewResultSchema.model_validate(result)
    except ValidationError as error:
        raise SchemaValidationError(_review_error_message(error)) from error


def validate_developer_result(result: dict[str, Any]) -> DeveloperResultSchema:
    """Validate and return a canonical developer result."""

    try:
        return DeveloperResultSchema.model_validate(result)
    except ValidationError as error:
        message = 'developer result does not match the canonical schema'
        raise SchemaValidationError(message) from error

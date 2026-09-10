"""Strict canonical schemas for vendor-neutral workflow messages."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from agent_orchestra.reviewer_paths import REVIEWER_ID_PATTERN


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


class ReviewerExecutionSchema(StrictSchema):
    """Persist one required reviewer in an immutable batch plan."""

    reviewer_id: str = Field(pattern=REVIEWER_ID_PATTERN.pattern)
    command: list[str] = Field(min_length=1)
    identity: InvocationIdentityRecordSchema
    timeout_seconds: int = Field(gt=0)


class ReviewerExecutionPlanSchema(StrictSchema):
    """Persist one ordered required-reviewer execution plan."""

    schema_version: Literal[1]
    reviewer_set_id: str = Field(pattern=REVIEWER_ID_PATTERN.pattern)
    aggregation_policy: Literal['all_required']
    reviewers: list[ReviewerExecutionSchema] = Field(min_length=2)

    @model_validator(mode='after')
    def validate_unique_reviewer_ids(self) -> ReviewerExecutionPlanSchema:
        """Reject a plan whose reviewers cannot own distinct evidence."""

        reviewer_ids = [reviewer.reviewer_id for reviewer in self.reviewers]
        if len(reviewer_ids) != len(set(reviewer_ids)):
            message = 'reviewer execution plan contains duplicate reviewer IDs'
            raise ValueError(message)
        return self


class ExecutionRecordBaseSchema(StrictSchema):
    """Fields shared by versioned resumable execution records."""

    run_id: str
    objective: str = Field(min_length=1)
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


class ExecutionRecordSchema(ExecutionRecordBaseSchema):
    """Schema-2 execution context for one resumable reviewer."""

    schema_version: Literal[2]
    reviewer: ExecutionRoleSchema


class ReviewerSetExecutionRecordSchema(ExecutionRecordBaseSchema):
    """Schema-3 execution context for one resumable reviewer set."""

    schema_version: Literal[3]
    reviewer_plan: ReviewerExecutionPlanSchema


ExecutionRecord = Annotated[
    ExecutionRecordSchema | ReviewerSetExecutionRecordSchema,
    Field(discriminator='schema_version'),
]
EXECUTION_RECORD_ADAPTER: TypeAdapter[ExecutionRecord] = TypeAdapter(ExecutionRecord)


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


class ReviewerBatchMemberResultSchema(StrictSchema):
    """Bind one required reviewer outcome to its canonical result when present."""

    reviewer_id: str = Field(pattern=REVIEWER_ID_PATTERN.pattern)
    outcome: Literal['approved', 'changes_requested', 'blocked', 'incomplete']
    result_path: str | None

    @model_validator(mode='after')
    def validate_result_presence(self) -> ReviewerBatchMemberResultSchema:
        """Bind completed outcomes to evidence and incomplete outcomes to its absence."""

        completed = self.outcome != 'incomplete'
        if completed != (self.result_path is not None):
            message = 'review batch member result path is inconsistent with outcome'
            raise ValueError(message)
        return self


class ReviewerBatchFindingSchema(ReviewFindingSchema):
    """Preserve one source finding and its reviewer in an aggregate batch."""

    reviewer_id: str = Field(pattern=REVIEWER_ID_PATTERN.pattern)
    source_finding_id: str


class ReviewerBatchResultBaseSchema(StrictSchema):
    """Fields shared by versioned aggregate reviewer-batch decisions."""

    schema_version: Literal[1, 2, 3]
    run_id: str
    iteration: int = Field(gt=0)
    reviewer_set_id: str = Field(pattern=REVIEWER_ID_PATTERN.pattern)
    aggregation_policy: Literal['all_required']
    diff_digest: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    verdict: Literal['approved', 'changes_requested', 'blocked']
    reviewers: list[ReviewerBatchMemberResultSchema] = Field(min_length=2)
    changes_requested_by: list[str]
    blocked_by: list[str]
    incomplete_reviewers: list[str]

    @model_validator(mode='after')
    def validate_rationale(self) -> ReviewerBatchResultBaseSchema:
        """Require ordered unique members and rationale derived from their outcomes."""

        reviewer_ids = [reviewer.reviewer_id for reviewer in self.reviewers]
        if len(reviewer_ids) != len(set(reviewer_ids)):
            message = 'review batch result contains duplicate reviewer IDs'
            raise ValueError(message)
        expected = {
            'changes_requested_by': [
                item.reviewer_id
                for item in self.reviewers
                if item.outcome == 'changes_requested'
            ],
            'blocked_by': [
                item.reviewer_id for item in self.reviewers if item.outcome == 'blocked'
            ],
            'incomplete_reviewers': [
                item.reviewer_id
                for item in self.reviewers
                if item.outcome == 'incomplete'
            ],
        }
        for field, value in expected.items():
            if getattr(self, field) != value:
                raise ValueError(f'review batch result has inconsistent {field}')
        expected_verdict = (
            'blocked'
            if expected['blocked_by'] or expected['incomplete_reviewers']
            else 'changes_requested'
            if expected['changes_requested_by']
            else 'approved'
        )
        if self.verdict != expected_verdict:
            message = 'review batch result has inconsistent verdict'
            raise ValueError(message)
        return self


class ReviewerBatchResultSchema(ReviewerBatchResultBaseSchema):
    """Legacy schema-1 aggregate decision for one immutable reviewer batch."""

    schema_version: Literal[1]


class ReviewerBatchResultWithFindingsSchema(ReviewerBatchResultBaseSchema):
    """Shared validation for aggregate decisions with reviewer findings."""

    schema_version: Literal[2, 3]
    findings: list[ReviewerBatchFindingSchema]

    @model_validator(mode='after')
    def validate_findings(self) -> ReviewerBatchResultWithFindingsSchema:
        """Require unique findings owned by reviewers that requested changes."""

        finding_ids = [finding.finding_id for finding in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            message = 'review batch result finding IDs must be unique'
            raise ValueError(message)
        for finding in self.findings:
            if (
                finding.reviewer_id not in self.changes_requested_by
                or finding.finding_id
                != f'{finding.reviewer_id}:{finding.source_finding_id}'
            ):
                message = 'review batch result contains an uncorrelated finding'
                raise ValueError(message)
        return self


class ReviewerBatchResultV2Schema(ReviewerBatchResultWithFindingsSchema):
    """Schema-2 aggregate decision with reviewer-qualified findings."""

    schema_version: Literal[2]


class ReviewerBatchResultSchemaV3(ReviewerBatchResultWithFindingsSchema):
    """Schema-3 aggregate decision addressable by developer remediation."""

    schema_version: Literal[3]
    message_id: str
    artifact_path: str

    @field_validator('message_id')
    @classmethod
    def validate_message_id(cls, value: str) -> str:
        """Require the aggregate correlation identity to be a UUID."""

        UUID(value)
        return value


ReviewerBatchResult = Annotated[
    ReviewerBatchResultSchema
    | ReviewerBatchResultV2Schema
    | ReviewerBatchResultSchemaV3,
    Field(discriminator='schema_version'),
]
REVIEWER_BATCH_RESULT_ADAPTER: TypeAdapter[ReviewerBatchResult] = TypeAdapter(
    ReviewerBatchResult
)


class IssueReviewFindingSchema(StrictSchema):
    """One actionable finding about issue prose or metadata."""

    finding_id: str = Field(min_length=1)
    dimension: Literal[
        'problem_clarity',
        'scope',
        'constraints',
        'dependencies',
        'risks',
        'acceptance_criteria',
        'testability',
        'implementation_readiness',
    ]
    severity: Literal['critical', 'high', 'medium', 'low']
    title: str = Field(min_length=1)
    section: str | None
    explanation: str = Field(min_length=1)
    suggested_change: str = Field(min_length=1)


class IssueReviewResultSchema(StrictSchema):
    """Canonical result shared by issue-review runtime adapters."""

    schema_version: Literal[1]
    source_digest: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    verdict: Literal['ready', 'changes_requested', 'blocked']
    summary: str = Field(min_length=1)
    findings: list[IssueReviewFindingSchema]
    validation: list[str]
    verification_gaps: list[str]

    @model_validator(mode='after')
    def validate_verdict_and_findings(self) -> IssueReviewResultSchema:
        """Enforce readiness consistency and unique finding identifiers."""

        if self.verdict == 'ready' and self.findings:
            message = 'ready issue review cannot contain findings'
            raise ValueError(message)
        if self.verdict == 'changes_requested' and not self.findings:
            message = 'changes_requested issue review requires findings'
            raise ValueError(message)
        identifiers = [finding.finding_id for finding in self.findings]
        if len(identifiers) != len(set(identifiers)):
            message = 'issue review finding IDs must be unique'
            raise ValueError(message)
        return self


class IssueSourceSchema(StrictSchema):
    """Canonical provider-neutral issue snapshot."""

    schema_version: Literal[1]
    provider: Literal['github', 'gitlab']
    host: str = Field(min_length=1)
    url: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    project: str = Field(min_length=1)
    issue_number: int = Field(gt=0)
    title: str
    body: str
    author: str = Field(min_length=1)
    labels: list[str]
    state: str = Field(min_length=1)
    created_at: str
    updated_at: str
    source_digest: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')


class IssueReviewRequestSchema(StrictSchema):
    """Versioned vendor-neutral request for reviewing one issue snapshot."""

    schema_version: Literal[1]
    job_id: str = Field(min_length=1)
    iteration: int = Field(gt=0)
    objective: str = Field(min_length=1)
    allowed_actions: list[Literal['read_issue_snapshot', 'write_review_evidence']]
    source: IssueSourceSchema
    prior_review: IssueReviewResultSchema | None


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
ISSUE_REVIEW_RESULT_SCHEMA = IssueReviewResultSchema.model_json_schema()
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


def validate_issue_review_result(result: dict[str, Any]) -> IssueReviewResultSchema:
    """Validate and return one canonical issue-review result."""

    try:
        return IssueReviewResultSchema.model_validate(result)
    except ValidationError as error:
        message = 'issue review result does not match the canonical schema'
        raise SchemaValidationError(message) from error


def validate_developer_result(result: dict[str, Any]) -> DeveloperResultSchema:
    """Validate and return a canonical developer result."""

    try:
        return DeveloperResultSchema.model_validate(result)
    except ValidationError as error:
        message = 'developer result does not match the canonical schema'
        raise SchemaValidationError(message) from error

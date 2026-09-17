"""Single source of truth for the structured Worker report wire contract.

The contract has exactly two layers, and they must agree:

```text
Worker is instructed to output:      REPORT_ENVELOPE_INSTRUCTION
        ↓
Extractor accepts:                   extract_report_candidate
        ↓
Parser constructs / Validator requires: structured_report.parse_structured_worker_report
        ↓
ExecutionReport persists:            worker.parsing.build_execution_report
```

This module owns the envelope half.  It never carries raw model output beyond
the call that produced it, and every failure it raises is typed, bounded, and
secret-free.
"""

from dataclasses import dataclass
from enum import StrEnum
import json


# A Worker final message is one JSON object.  Bounded so a runaway response can
# never make Code Mule parse an unbounded string.
MAX_REPORT_MESSAGE_LENGTH = 200_000

REPORT_ENVELOPE_INSTRUCTION = (
    "Final answer format (strict):\n"
    "- Your entire final answer must be exactly one JSON object.\n"
    "- Do not add prose, headings, explanations, or Markdown around it.\n"
    "- Do not wrap it in a ```json code fence.\n"
    "- The object's top-level fields must be exactly: status, summary, "
    "files_changed, tests, static_checks, git_state, issues, human_action.\n"
    "- Emit no extra top-level fields and omit none."
)


class ReportFailureStage(StrEnum):
    """Which layer of the report contract rejected the Worker answer."""

    ENVELOPE = "envelope"
    EXTRACTION = "extraction"
    JSON_DECODE = "json_decode"
    SCHEMA = "schema"
    SEMANTIC_VALIDATION = "semantic_validation"


class ReportValidationCode(StrEnum):
    """Bounded reason a Worker report was rejected."""

    NOT_A_STRING = "not_a_string"
    EMPTY_OUTPUT = "empty_output"
    OUTPUT_TOO_LARGE = "output_too_large"
    NO_JSON_CANDIDATE = "no_json_candidate"
    AMBIGUOUS_JSON_CANDIDATE = "ambiguous_json_candidate"
    INVALID_JSON = "invalid_json"
    NOT_AN_OBJECT = "not_an_object"
    MISSING_FIELD = "missing_field"
    EXTRA_FIELD = "extra_field"
    INVALID_ENUM = "invalid_enum"
    INVALID_FIELD_TYPE = "invalid_field_type"
    INVALID_CHECK_RESULT = "invalid_check_result"
    INVALID_HUMAN_ACTION = "invalid_human_action"
    INVALID_SEMANTIC_VALUE = "invalid_semantic_value"


class ReportExtractionMode(StrEnum):
    """How the report candidate was obtained from the final message."""

    WHOLE_MESSAGE = "whole_message"
    CODE_FENCE = "code_fence"
    EMBEDDED_OBJECT = "embedded_object"


class ReportContractError(ValueError):
    """Typed, bounded rejection raised by the report contract layer."""

    def __init__(
        self,
        stage: ReportFailureStage,
        code: ReportValidationCode,
        *,
        field_path: str | None = None,
        candidate_found: bool = False,
        json_decoded: bool = False,
        semantic_validation_started: bool = False,
    ) -> None:
        if not isinstance(stage, ReportFailureStage):
            raise ValueError("report failure stage must be typed")
        if not isinstance(code, ReportValidationCode):
            raise ValueError("report validation code must be typed")
        self.stage = stage
        self.code = code
        self.field_path = field_path
        self.candidate_found = bool(candidate_found)
        self.json_decoded = bool(json_decoded)
        self.semantic_validation_started = bool(semantic_validation_started)
        super().__init__(self.describe())

    def describe(self) -> str:
        """Bounded, secret-free description suitable for persistence."""

        where = "" if self.field_path is None else f" at {self.field_path}"
        return f"worker report {self.stage.value} failed: {self.code.value}{where}"


@dataclass(frozen=True)
class ExtractedReport:
    """One decoded JSON candidate plus how it was obtained."""

    value: object
    mode: ReportExtractionMode
    candidate_length: int


def _decode(text: str) -> tuple[bool, object]:
    try:
        return True, json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return False, None


def _fenced_blocks(text: str) -> tuple[str, ...]:
    """Return the bodies of fenced blocks whose info string is json or empty."""

    blocks: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            info = stripped[3:].strip().lower()
            if info in {"", "json"}:
                body: list[str] = []
                index += 1
                while index < len(lines) and not lines[index].strip().startswith(
                    marker
                ):
                    body.append(lines[index])
                    index += 1
                if body:
                    blocks.append("\n".join(body))
        index += 1
    return tuple(blocks)


def _balanced_objects(text: str) -> tuple[str, ...]:
    """Return every top-level balanced ``{...}`` span, ignoring string content."""

    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start : index + 1])
                start = -1
    return tuple(spans)


def extract_report_candidate(raw_output: object) -> ExtractedReport:
    """Extract exactly one JSON candidate from one Worker final message.

    Order is fixed and deterministic:

    1. the whole message must be JSON;
    2. otherwise a single fenced ```json block;
    3. otherwise a single embedded balanced JSON object.

    More than one distinct candidate fails closed: Code Mule never guesses
    between two plausible reports.
    """

    if not isinstance(raw_output, str):
        raise ReportContractError(
            ReportFailureStage.ENVELOPE, ReportValidationCode.NOT_A_STRING
        )
    text = raw_output.strip()
    if text == "":
        raise ReportContractError(
            ReportFailureStage.ENVELOPE, ReportValidationCode.EMPTY_OUTPUT
        )
    if len(text) > MAX_REPORT_MESSAGE_LENGTH:
        raise ReportContractError(
            ReportFailureStage.ENVELOPE, ReportValidationCode.OUTPUT_TOO_LARGE
        )

    decoded, value = _decode(text)
    if decoded:
        return ExtractedReport(value, ReportExtractionMode.WHOLE_MESSAGE, len(text))

    fenced = _fenced_blocks(text)
    if len(fenced) == 1:
        body = fenced[0].strip()
        decoded, value = _decode(body)
        if not decoded:
            raise ReportContractError(
                ReportFailureStage.JSON_DECODE,
                ReportValidationCode.INVALID_JSON,
                field_path="code_fence_body",
                candidate_found=True,
            )
        return ExtractedReport(value, ReportExtractionMode.CODE_FENCE, len(body))
    if len(fenced) > 1:
        raise ReportContractError(
            ReportFailureStage.EXTRACTION,
            ReportValidationCode.AMBIGUOUS_JSON_CANDIDATE,
            field_path="code_fence_blocks",
            candidate_found=True,
        )

    objects = _balanced_objects(text)
    if not objects:
        raise ReportContractError(
            ReportFailureStage.EXTRACTION, ReportValidationCode.NO_JSON_CANDIDATE
        )
    if len(objects) > 1:
        raise ReportContractError(
            ReportFailureStage.EXTRACTION,
            ReportValidationCode.AMBIGUOUS_JSON_CANDIDATE,
            field_path="embedded_objects",
            candidate_found=True,
        )
    body = objects[0]
    decoded, value = _decode(body)
    if not decoded:
        raise ReportContractError(
            ReportFailureStage.JSON_DECODE,
            ReportValidationCode.INVALID_JSON,
            field_path="embedded_object",
            candidate_found=True,
        )
    return ExtractedReport(value, ReportExtractionMode.EMBEDDED_OBJECT, len(body))


__all__ = [
    "ExtractedReport",
    "MAX_REPORT_MESSAGE_LENGTH",
    "REPORT_ENVELOPE_INSTRUCTION",
    "ReportContractError",
    "ReportExtractionMode",
    "ReportFailureStage",
    "ReportValidationCode",
    "extract_report_candidate",
]

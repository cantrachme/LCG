"""Deterministic extraction of the labeled training-contract PDF text layer."""

import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Generic, Literal, TypeVar

import pymupdf
from pydantic import BaseModel

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_PAGES = 10
T = TypeVar("T")


class SourceField(BaseModel, Generic[T]):
    original: str | None = None
    value: T | None = None
    status: Literal["extracted", "missing", "needs_review"] = "missing"
    source_page: int | None = None


class Identity(BaseModel):
    child_name: SourceField[str]
    child_id: SourceField[str]
    family_name: SourceField[str]
    family_id: SourceField[str]
    center: SourceField[str]
    agency: SourceField[str]
    agency_note: SourceField[str]


class Authorization(BaseModel):
    start_date: SourceField[date]
    end_date: SourceField[date]
    child_contract_id: SourceField[str]
    case_number: SourceField[str]
    case_worker: SourceField[str]
    lcw_contract_number: SourceField[str]


class Attendance(BaseModel):
    days: list[str]
    start_time: time
    end_time: time


class Schedule(BaseModel):
    classroom: SourceField[str]
    program: SourceField[str]
    attendance: SourceField[Attendance]
    hours_per_week: SourceField[Decimal]
    five_day_tuition: SourceField[Decimal]


class UnitTier(BaseModel):
    minimum_hours: Decimal
    maximum_hours: Decimal | None  # None means no upper bound in a "25+" tier.


class RateTier(BaseModel):
    unit_tier: SourceField[UnitTier]
    weekly_unit_rate: SourceField[Decimal]
    weekly_copay: SourceField[Decimal]


class Billing(BaseModel):
    reimbursement_type: SourceField[str]
    copay_type: SourceField[str]
    unit_category: SourceField[str]
    maximum_setting: SourceField[str]
    maximum_amount: SourceField[Decimal]
    over_market_setting: SourceField[str]
    over_market_amount: SourceField[Decimal]
    variable_schedule_based_on_attendance: SourceField[bool]
    absent_day_eligibility: SourceField[bool]
    restricted_hours: SourceField[bool]
    absence_days_at_previous_provider: SourceField[int]


class ExistingPRA(BaseModel):
    start_date: SourceField[date]
    end_date: SourceField[date]
    instruction: SourceField[str]
    reuse_required: SourceField[bool]


class DocumentInfo(BaseModel):
    training_only: SourceField[bool]
    training_terms: SourceField[str]
    authorization_note: SourceField[str]


class OriginalContract(BaseModel):
    identity: Identity
    authorization: Authorization
    schedule: Schedule
    part_time: RateTier
    full_time: RateTier
    billing: Billing
    existing_pra: ExistingPRA
    document: DocumentInfo


class ExtractionWarning(BaseModel):
    code: str
    message: str


class ContractExtraction(BaseModel):
    extraction_id: str | None = None
    page_count: int
    original: OriginalContract
    warnings: list[ExtractionWarning]


class ExtractionError(ValueError):
    """A safe, user-facing error; never includes document contents."""


DATE = r"(?:\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})"
DASH = r"[–—-]"
MISSING = {"", "n/a", "not provided", "unknown", "not available"}


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%m/%d/%Y" if "/" in value else "%Y-%m-%d").date()


def number(value: str) -> Decimal:
    if not re.fullmatch(r"\$?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", value):
        raise ValueError
    return Decimal(value.removeprefix("$").replace(",", ""))


def boolean(value: str) -> bool:
    if value.lower() not in {"yes", "no"}:
        raise ValueError
    return value.lower() == "yes"


def attendance(value: str) -> Attendance:
    match = re.fullmatch(rf"(\d{{1,2}}:\d{{2}}\s*[AP]M)\s*{DASH}\s*(\d{{1,2}}:\d{{2}}\s*[AP]M)", value, re.I)
    if not match:
        raise ValueError
    start, end = [datetime.strptime(part.upper(), "%I:%M %p").time() for part in match.groups()]
    if end <= start:
        raise ValueError
    return Attendance(days=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], start_time=start, end_time=end)


def unit_tier(value: str) -> UnitTier:
    match = re.fullmatch(rf"(\d+(?:\.\d+)?)(?:\s*{DASH}\s*(\d+(?:\.\d+)?)|(\+))\s*hrs", value, re.I)
    if not match:
        raise ValueError
    low = Decimal(match[1])
    high = Decimal(match[2]) if match[2] else None
    if high is not None and high < low:
        raise ValueError
    return UnitTier(minimum_hours=low, maximum_hours=high)


def extract_contract(content: bytes) -> ContractExtraction:
    if not content.lstrip().startswith(b"%PDF-"):
        raise ExtractionError("The file is not a valid PDF. Choose a text-based PDF contract.")
    try:
        with pymupdf.open(stream=content, filetype="pdf") as document:
            if document.needs_pass:
                raise ExtractionError("Password-protected PDFs are not supported. Upload an unlocked PDF.")
            if not 1 <= len(document) <= MAX_PAGES:
                raise ExtractionError(f"Upload a contract with 1–{MAX_PAGES} pages.")
            pages = [page.get_text("text", sort=True) for page in document]
    except ExtractionError:
        raise
    except (pymupdf.FileDataError, RuntimeError, ValueError):
        raise ExtractionError("The PDF could not be read. Upload an undamaged, text-based PDF.") from None

    if any(not text.strip() for text in pages):
        raise ExtractionError("The PDF contains a page without readable text. Scanned/image-only pages need OCR, which is not supported yet.")
    if sum(map(len, pages)) > 100_000:
        raise ExtractionError("The PDF contains too much text for this contract format.")
    if not any(re.search(r"\bCHILD CARE SUBSIDY CONTRACT\b", text, re.I) for text in pages):
        raise ExtractionError("Unsupported document. Upload the labeled Child Care Subsidy Contract format.")

    warnings: list[ExtractionWarning] = []
    # Match complete labeled lines only. Never fill absent fields from fixture data.
    def field(label: str, convert=str) -> SourceField:
        matches = []
        for page_number, text in enumerate(pages, 1):
            for match in re.finditer(rf"^[ \t]*(?:{label})[ \t]*:[ \t]*([^\n]*)[ \t]*$", text, re.I | re.M):
                matches.append((match[1].strip(), page_number))
        if not matches:
            return SourceField()
        original, page_number = matches[0]
        if len({value for value, _ in matches}) > 1:
            return SourceField(original=" | ".join(value for value, _ in matches), status="needs_review", source_page=page_number)
        if original.lower() in MISSING:
            return SourceField(original=original, source_page=page_number)
        try:
            value = convert(original)
        except (ValueError, ArithmeticError):
            return SourceField(original=original, status="needs_review", source_page=page_number)
        return SourceField(original=original, value=value, status="extracted", source_page=page_number)

    def dates(value: str, index: int) -> date:
        match = re.fullmatch(rf"({DATE})\s*{DASH}\s*({DATE})", value)
        if not match:
            raise ValueError
        return parse_date(match[index + 1])

    def setting(value: str, index: int):
        parts = value.rsplit("/", 1)
        if len(parts) != 2 or not parts[0].strip():
            raise ValueError
        return parts[0].strip() if index == 0 else number(parts[1].strip())

    def hours(value: str) -> Decimal:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*hours/week", value, re.I)
        if not match or Decimal(match[1]) > 168:
            raise ValueError
        return Decimal(match[1])

    def absence_days(value: str) -> int:
        if not re.fullmatch(r"\d+", value):
            raise ValueError
        return int(value)

    def reuse(value: str) -> bool:
        if not re.fullmatch(r"Reuse this PRA\.\s*Do not create a duplicate\.", value, re.I):
            raise ValueError
        return True

    def training(value: str) -> bool:
        if not re.fullmatch(rf"SAMPLE\s*{DASH}\s*NON-PRODUCTION TRAINING ONLY", value, re.I):
            raise ValueError
        return True

    def tier(prefix: str) -> RateTier:
        return RateTier(
            unit_tier=field(f"{prefix} unit tier", unit_tier),
            weekly_unit_rate=field(f"{prefix} Weekly Unit Rate", number),
            weekly_copay=field(f"{prefix} Weekly Copay", number),
        )

    lcw_number = field("LCW contract number")
    if lcw_number.original and re.fullmatch(r"assigned by LCW after you save\.", lcw_number.original, re.I):
        lcw_number = SourceField(original=lcw_number.original, source_page=lcw_number.source_page)

    original = OriginalContract(
        identity=Identity(
            child_name=field("Child"), child_id=field("Child ID"), family_name=field("Family"),
            family_id=field("Family ID"), center=field("Center"), agency=field("LCW agency selection"),
            agency_note=field("Agency note"),
        ),
        authorization=Authorization(
            start_date=field("Start", parse_date), end_date=field("Stop", parse_date),
            child_contract_id=field(r"Child Contract ID#"), case_number=field(r"Case #"),
            case_worker=field("Case Worker"), lcw_contract_number=lcw_number,
        ),
        schedule=Schedule(
            classroom=field("Classroom"), program=field("Program"),
            attendance=field(rf"Monday\s*{DASH}\s*Friday", attendance), hours_per_week=field("Scheduled", hours),
            five_day_tuition=field("Five-day tuition", number),
        ),
        part_time=tier("Part-time"), full_time=tier("Full-time"),
        billing=Billing(
            reimbursement_type=field("Reimbursement type"), copay_type=field("Copay type"),
            unit_category=field("Unit category"), maximum_setting=field("Maximum", lambda value: setting(value, 0)),
            maximum_amount=field("Maximum", lambda value: setting(value, 1)),
            over_market_setting=field("Over market", lambda value: setting(value, 0)),
            over_market_amount=field("Over market", lambda value: setting(value, 1)),
            variable_schedule_based_on_attendance=field("Variable Schedule Billing Based on Attendance", boolean),
            absent_day_eligibility=field("Absent Day Eligibility", boolean), restricted_hours=field("Restricted Hours", boolean),
            absence_days_at_previous_provider=field("Absence Days at Previous Provider", absence_days),
        ),
        existing_pra=ExistingPRA(
            start_date=field("Existing PRA dates", lambda value: dates(value, 0)),
            end_date=field("Existing PRA dates", lambda value: dates(value, 1)),
            instruction=field("PRA note"), reuse_required=field("PRA note", reuse),
        ),
        document=DocumentInfo(
            training_only=field("Document status", training), training_terms=field("Training terms note"),
            authorization_note=field("Authorization note"),
        ),
    )

    if not any(item.status != "missing" for _, item in original.identity):
        raise ExtractionError("The PDF title was recognized, but its field layout is unsupported. Upload the labeled training-contract format.")
    if original.document.training_only.value:
        warnings.append(ExtractionWarning(code="training_only", message="Non-production training contract. The fictional terms are not a legal or payment authorization."))
    else:
        warnings.append(ExtractionWarning(code="training_status_unconfirmed", message="The non-production training designation could not be confirmed. Review the document before using its values."))
    if original.existing_pra.reuse_required.value:
        warnings.append(ExtractionWarning(code="reuse_existing_pra", message="The source requires reusing the existing PRA. Do not create a duplicate; its portal state has not been verified."))
    else:
        warnings.append(ExtractionWarning(code="pra_instruction_unresolved", message="PRA reuse instructions are missing or unrecognized. Review the original instruction; no new PRA is assumed."))

    for name in ("authorization", "existing_pra"):
        period = getattr(original, name)
        if period.start_date.value and period.end_date.value and period.start_date.value > period.end_date.value:
            period.start_date.status = period.end_date.status = "needs_review"
            warnings.append(ExtractionWarning(code=f"{name}_date_order", message=f"The {name.replace('_', ' ')} start date is after its end date. Review both source dates."))
    contract, pra = original.authorization, original.existing_pra
    if all(item.status == "extracted" for item in (contract.start_date, contract.end_date, pra.start_date, pra.end_date)):
        if contract.start_date.value < pra.start_date.value or contract.end_date.value > pra.end_date.value:
            warnings.append(ExtractionWarning(code="pra_coverage", message="The contract dates extend outside the existing PRA dates. Review the coverage conflict."))
    if any(item.status == "needs_review" for _, section in original for _, item in section):
        warnings.append(ExtractionWarning(code="fields_need_review", message="Some values are ambiguous or could not be normalized. Review the fields marked Needs review."))
    return ContractExtraction(page_count=len(pages), original=original, warnings=warnings)

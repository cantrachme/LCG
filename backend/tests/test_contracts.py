"""Synthetic PDF fixtures are generated in memory; no uploaded files are saved."""

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.contracts import MAX_FILE_BYTES
from app.main import app

client = TestClient(app)
SYNTHETIC_CONTRACT = """Document status: SAMPLE - NON-PRODUCTION TRAINING ONLY
Document title: CHILD CARE SUBSIDY CONTRACT
Child: Example Child
Child ID: 001234
Family: Example
Family ID: 005678
Center: 0001-Test Center
LCW agency selection: Fictional Training Agency
Agency note: Not agency-issued.
Start: 09/01/2026
Stop: 09/30/2026
Child Contract ID#: TEST-CONTRACT
Case #: TEST-CASE
Case Worker: TRAINING ONLY
Classroom: Example Classroom
Program: Example Program
Monday-Friday: 1:15 PM-6:00 PM
Scheduled: 23.75 hours/week
Five-day tuition: $180.00
Existing PRA dates: 08/01/2026-11/30/2026
PRA note: Reuse this PRA. Do not create a duplicate.
Part-time unit tier: 0-24.99 hrs
Part-time Weekly Unit Rate: $160.00
Part-time Weekly Copay: $20.00
Full-time unit tier: 25+ hrs
Full-time Weekly Unit Rate: $180.00
Full-time Weekly Copay: $0.00
Reimbursement type: Weekly
Copay type: Weekly
Unit category: Weekly
Maximum: Agency Maximum Dollar - Weekly / $180.00
Over market: Partial / $0.00
Variable Schedule Billing Based on Attendance: Yes
Absent Day Eligibility: Yes
Restricted Hours: No
Absence Days at Previous Provider: 0
Training terms note: Fictional training terms.
Authorization note: Not a legal agreement or payment authorization.
LCW contract number: assigned by LCW after you save.
"""


def pdf(text=SYNTHETIC_CONTRACT, *, pages=1, encrypted=False):
    with pymupdf.open() as document:
        for _ in range(pages):
            page = document.new_page()
            page.insert_text((30, 30), text, fontsize=9)
        options = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "owner_pw": "test-owner", "user_pw": "test-user"} if encrypted else {}
        return document.tobytes(**options)


def upload(content, filename="training.pdf", content_type="application/pdf"):
    return client.post("/api/contracts/extract", headers={"X-LCG-Local": "1"}, files={"file": (filename, content, content_type)})


def test_extracts_original_and_normalized_contract():
    response = upload(pdf())
    assert response.status_code == 200
    data = response.json()
    original = data["original"]
    assert data["page_count"] == 1
    assert original["identity"]["child_id"]["value"] == "001234"
    assert original["identity"]["family_id"]["value"] == "005678"
    assert original["authorization"]["start_date"] == {
        "original": "09/01/2026", "value": "2026-09-01", "status": "extracted", "source_page": 1,
    }
    assert original["schedule"]["attendance"]["value"] == {
        "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        "start_time": "13:15:00", "end_time": "18:00:00",
    }
    assert original["schedule"]["hours_per_week"]["value"] == "23.75"
    assert original["schedule"]["five_day_tuition"]["value"] == "180.00"
    assert original["part_time"]["unit_tier"]["value"] == {"minimum_hours": "0", "maximum_hours": "24.99"}
    assert original["full_time"]["unit_tier"]["value"] == {"minimum_hours": "25", "maximum_hours": None}
    assert original["part_time"]["weekly_unit_rate"]["value"] == "160.00"
    assert original["part_time"]["weekly_copay"]["value"] == "20.00"
    assert original["full_time"]["weekly_unit_rate"]["value"] == "180.00"
    assert original["full_time"]["weekly_copay"]["value"] == "0.00"
    billing = original["billing"]
    assert billing["maximum_setting"]["value"] == "Agency Maximum Dollar - Weekly"
    assert billing["maximum_amount"]["value"] == "180.00"
    assert billing["over_market_setting"]["value"] == "Partial"
    assert billing["over_market_amount"]["value"] == "0.00"
    assert billing["restricted_hours"]["value"] is False
    assert billing["absent_day_eligibility"]["value"] is True
    assert billing["variable_schedule_based_on_attendance"]["value"] is True
    assert billing["absence_days_at_previous_provider"]["value"] == 0
    pra = original["existing_pra"]
    assert pra["reuse_required"]["value"] is True
    assert pra["instruction"]["original"] == "Reuse this PRA. Do not create a duplicate."
    assert pra["start_date"]["value"] == "2026-08-01"
    assert pra["end_date"]["value"] == "2026-11-30"
    assert original["authorization"]["lcw_contract_number"]["value"] is None
    assert original["authorization"]["lcw_contract_number"]["status"] == "missing"
    assert {warning["code"] for warning in data["warnings"]} == {"training_only", "reuse_existing_pra"}
    assert sum(field["status"] == "extracted" for section in original.values() for field in section.values()) == 41
    assert "modifications" not in data


@pytest.mark.parametrize("replacement", ["", "Case Worker: \n", "Case Worker: Not provided\n"])
def test_missing_fields_are_explicit_and_do_not_consume_next_label(replacement):
    data = upload(pdf(SYNTHETIC_CONTRACT.replace("Case Worker: TRAINING ONLY\n", replacement))).json()
    field = data["original"]["authorization"]["case_worker"]
    assert field["status"] == "missing"
    assert field["value"] is None
    assert data["original"]["schedule"]["classroom"]["value"] == "Example Classroom"


@pytest.mark.parametrize("old,new,section,key", [
    ("09/01/2026", "02/30/2026", "authorization", "start_date"),
    ("$160.00", "$not-a-rate", "part_time", "weekly_unit_rate"),
    ("Restricted Hours: No", "Restricted Hours: Maybe", "billing", "restricted_hours"),
    ("23.75 hours/week", "200 hours/week", "schedule", "hours_per_week"),
    ("0-24.99 hrs", "30-20 hrs", "part_time", "unit_tier"),
    ("1:15 PM-6:00 PM", "6:00 PM-1:15 PM", "schedule", "attendance"),
])
def test_invalid_values_preserve_source_and_require_review(old, new, section, key):
    data = upload(pdf(SYNTHETIC_CONTRACT.replace(old, new))).json()
    field = data["original"][section][key]
    assert field["status"] == "needs_review"
    assert field["value"] is None
    assert field["original"]
    assert "fields_need_review" in {warning["code"] for warning in data["warnings"]}


def test_conflicting_duplicate_values_are_not_silently_chosen():
    data = upload(pdf(SYNTHETIC_CONTRACT + "Child ID: 009999\n")).json()
    field = data["original"]["identity"]["child_id"]
    assert field["status"] == "needs_review"
    assert field["value"] is None
    assert "001234" in field["original"] and "009999" in field["original"]


def test_unrecognized_pra_instruction_is_not_inferred():
    data = upload(pdf(SYNTHETIC_CONTRACT.replace("Reuse this PRA. Do not create a duplicate.", "Review the existing agreement."))).json()
    assert data["original"]["existing_pra"]["reuse_required"]["value"] is None
    assert data["original"]["existing_pra"]["instruction"]["original"] == "Review the existing agreement."
    assert "pra_instruction_unresolved" in {warning["code"] for warning in data["warnings"]}


def test_out_of_range_and_reversed_dates_warn_without_overwriting():
    data = upload(pdf(SYNTHETIC_CONTRACT.replace("09/30/2026", "02/10/2027"))).json()
    assert data["original"]["authorization"]["end_date"]["value"] == "2027-02-10"
    assert "pra_coverage" in {warning["code"] for warning in data["warnings"]}
    data = upload(pdf(SYNTHETIC_CONTRACT.replace("09/30/2026", "08/20/2026"))).json()
    assert data["original"]["authorization"]["end_date"]["status"] == "needs_review"
    assert "authorization_date_order" in {warning["code"] for warning in data["warnings"]}


@pytest.mark.parametrize("content,name,mime,status", [
    (b"", "empty.pdf", "application/pdf", 400),
    (b"text", "file.txt", "text/plain", 415),
    (b"text", "fake.pdf", "application/pdf", 422),
    (b"%PDF-1.7\nbroken", "broken.pdf", "application/pdf", 422),
    (b"x" * (MAX_FILE_BYTES + 1), "large.pdf", "application/pdf", 413),
])
def test_rejects_invalid_uploads(content, name, mime, status):
    response = upload(content, name, mime)
    assert response.status_code == status
    assert isinstance(response.json()["detail"], str)


@pytest.mark.parametrize("kind", ["blank", "unrelated", "unsupported_layout", "encrypted", "too_many_pages"])
def test_rejects_unsupported_documents(kind):
    contents = {
        "blank": lambda: pdf(""),
        "unrelated": lambda: pdf("Unrelated report"),
        "unsupported_layout": lambda: pdf("CHILD CARE SUBSIDY CONTRACT\nAn unsupported table layout"),
        "encrypted": lambda: pdf(encrypted=True),
        "too_many_pages": lambda: pdf(pages=11),
    }
    response = upload(contents[kind]())
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


def test_requires_file_and_accepts_generic_binary_pdf():
    assert client.post("/api/contracts/extract", headers={"X-LCG-Local": "1"}).status_code == 422
    assert upload(pdf(), "TRAINING.PDF", "application/octet-stream").status_code == 200


def test_cors_allows_frontend_post_and_rejects_other_origins():
    for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
        response = client.options("/api/contracts/extract", headers={
            "Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type",
        })
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        assert "POST" in response.headers["access-control-allow-methods"]
    response = client.options("/api/contracts/extract", headers={"Origin": "https://example.com", "Access-Control-Request-Method": "POST"})
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers

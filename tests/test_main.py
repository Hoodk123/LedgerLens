from datetime import date
from decimal import Decimal
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main
from app.db import Base, get_db
from app.extractor import ExtractedInvoice


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    database_path = tmp_path / "test.db"
    test_engine = create_engine(
        f"sqlite:///{database_path}", connect_args={"check_same_thread": False}
    )
    test_session = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)

    def override_get_db():
        with test_session() as db:
            yield db

    monkeypatch.setattr(main, "engine", test_engine)
    main.app.dependency_overrides[get_db] = override_get_db
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()
    test_engine.dispose()


def extracted_invoice() -> ExtractedInvoice:
    return ExtractedInvoice.model_validate(
        {
            "vendor": "Acme",
            "invoice_number": "A-100",
            "date": date.today().isoformat(),
            "line_items": [
                {
                    "description": "Widget",
                    "quantity": 2,
                    "unit_price": 5,
                    "total": 10,
                }
            ],
            "subtotal": 10,
            "tax_amount": 1,
            "total": 11,
            "currency": "USD",
        }
    )


def test_invoice_upload_review_list_detail_and_reports(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "extract_invoice", lambda image, mime: extracted_invoice())

    upload_response = client.post(
        "/invoices",
        files={"file": ("invoice.jpg", b"\xff\xd8\xffimage", "image/jpeg")},
    )

    assert upload_response.status_code == 201
    created = upload_response.json()
    assert created["vendor"] == "Acme"
    assert len(created["line_items"]) == 1
    assert {flag["rule"] for flag in created["flags"]} == {"new_vendor"}

    invoice_id = created["id"]
    filtered = client.get(
        "/invoices",
        params={
            "status": "pending_review",
            "vendor": "acme",
            "start_date": date.today().isoformat(),
            "end_date": date.today().isoformat(),
        },
    )
    assert [invoice["id"] for invoice in filtered.json()] == [invoice_id]

    detail = client.get(f"/invoices/{invoice_id}")
    assert detail.json()["line_items"][0]["description"] == "Widget"

    review = client.post(
        f"/invoices/{invoice_id}/review",
        json={"decision": "approve", "note": "Verified"},
    )
    assert review.json()["status"] == "approved"
    assert review.json()["flags"][0]["reviewer_note"] == "Verified"

    summary = client.get("/reports/summary").json()
    assert summary["spend_by_vendor"][0]["vendor"] == "Acme"
    assert Decimal(summary["spend_by_vendor"][0]["total"]) == Decimal("11.00")
    assert summary["spend_by_month"][0]["month"] == date.today().strftime("%Y-%m")
    assert summary["flagged_count"] == 0

    export = client.get("/reports/export.csv")
    assert export.status_code == 200
    assert "A-100" in export.text


def test_upload_rejects_mismatched_file_signature(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_called(image: bytes, mime_type: str) -> ExtractedInvoice:
        raise AssertionError("Extractor must not receive an invalid upload.")

    monkeypatch.setattr(main, "extract_invoice", fail_if_called)

    response = client.post(
        "/invoices",
        files={"file": ("invoice.jpg", b"not a jpeg", "image/jpeg")},
    )

    assert response.status_code == 415


def test_failed_extraction_is_saved_for_manual_review(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_extraction(image: bytes, mime_type: str) -> ExtractedInvoice:
        raise main.ExtractionError("Invalid invoice JSON.")

    monkeypatch.setattr(main, "extract_invoice", fail_extraction)

    response = client.post(
        "/invoices",
        files={"file": ("invoice.png", b"\x89PNG\r\n\x1a\nimage", "image/png")},
    )

    assert response.status_code == 201
    invoice = response.json()
    assert invoice["status"] == "pending_review"
    assert any(flag["rule"] == "extraction_failed" for flag in invoice["flags"])


def test_upload_rejects_file_over_five_megabytes(client: TestClient) -> None:
    oversized_file = b"\xff\xd8\xff" + b"x" * (5 * 1024 * 1024)

    response = client.post(
        "/invoices",
        files={"file": ("invoice.jpg", oversized_file, "image/jpeg")},
    )

    assert response.status_code == 413


def test_pdf_upload_uses_pdf_mime_type(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    received_mime_types: list[str] = []

    def fake_extract(image: bytes, mime_type: str) -> ExtractedInvoice:
        received_mime_types.append(mime_type)
        return extracted_invoice()

    monkeypatch.setattr(main, "extract_invoice", fake_extract)

    response = client.post(
        "/invoices",
        files={"file": ("invoice.pdf", b"%PDF-1.7 data", "application/pdf")},
    )

    assert response.status_code == 201
    assert received_mime_types == ["application/pdf"]
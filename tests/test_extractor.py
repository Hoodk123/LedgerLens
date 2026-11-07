import base64

import pytest

from app import extractor

VALID_RESPONSE = (
    '{"vendor":"Acme","invoice_number":"A-1","date":"2026-01-02",'
    '"line_items":[{"description":"Widget","quantity":2,"unit_price":3.5,"total":7}],'
    '"subtotal":7,"tax_amount":0,"total":7,"currency":"USD"}'
)


def test_build_messages_encodes_image_as_data_url() -> None:
    content = extractor.build_messages(b"abc", "image/png")[0]["content"]

    assert content[1]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(b"abc").decode("ascii")
    )


def test_parse_response_strips_code_fence_and_validates() -> None:
    invoice = extractor.parse_response(f"```json\n{VALID_RESPONSE}\n```")

    assert invoice.vendor == "Acme"
    assert invoice.date.isoformat() == "2026-01-02"
    assert invoice.line_items[0].total == 7


def test_extract_invoice_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(["not json", VALID_RESPONSE])
    calls = 0

    def fake_call_model(messages: list[dict[str, object]]) -> str:
        nonlocal calls
        calls += 1
        return next(responses)

    monkeypatch.setattr(extractor, "call_model", fake_call_model)

    invoice = extractor.extract_invoice(b"image")

    assert invoice.vendor == "Acme"
    assert calls == 2


def test_extract_invoice_raises_after_second_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_call_model(messages: list[dict[str, object]]) -> str:
        nonlocal calls
        calls += 1
        return "not json"

    monkeypatch.setattr(extractor, "call_model", fake_call_model)

    with pytest.raises(extractor.ExtractionError, match="after one retry"):
        extractor.extract_invoice(b"image")

    assert calls == 2
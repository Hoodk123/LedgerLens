from datetime import date, timedelta
from decimal import Decimal

from app.anomalies import (
	check_amount_outlier,
	check_date_oddity,
	check_duplicate,
	check_line_mismatch,
	check_math_mismatch,
	check_missing_fields,
	check_new_vendor,
	run_all_rules,
)
from app.models import Invoice, LineItem


def make_invoice(**values: object) -> Invoice:
	defaults: dict[str, object] = {
		"vendor": "Acme",
		"invoice_number": "A-1",
		"invoice_date": date.today(),
		"subtotal": Decimal("10.00"),
		"tax_amount": Decimal("0.00"),
		"total": Decimal("10.00"),
	}
	defaults.update(values)
	return Invoice(**defaults)


def make_history(size: int, **values: object) -> list[Invoice]:
	return [make_invoice(invoice_number=f"A-{index}", **values) for index in range(size)]


def test_duplicate_rule_matches_vendor_and_invoice_number() -> None:
	invoice = make_invoice()

	flag = check_duplicate(invoice, [make_invoice()])

	assert flag is not None
	assert flag.rule == "duplicate"
	assert flag.reason.endswith(".")


def test_math_mismatch_rule_compares_lines_plus_tax() -> None:
	invoice = make_invoice(total=Decimal("12.00"), tax_amount=Decimal("1.00"))
	invoice.line_items = [
		LineItem(description="Widget", quantity=Decimal("1"), unit_price=Decimal("10"), line_total=Decimal("10"))
	]

	flag = check_math_mismatch(invoice, [])

	assert flag is not None
	assert flag.rule == "math_mismatch"
	assert flag.reason.endswith(".")
	assert check_math_mismatch(make_invoice(total=Decimal("10.02")), []) is not None


def test_line_mismatch_rule_compares_quantity_and_price() -> None:
	invoice = make_invoice()
	invoice.line_items = [
		LineItem(description="Widget", quantity=Decimal("2"), unit_price=Decimal("3"), line_total=Decimal("5"))
	]

	flag = check_line_mismatch(invoice, [])

	assert flag is not None
	assert flag.rule == "line_mismatch"
	assert flag.reason.endswith(".")


def test_amount_outlier_requires_five_historical_invoices() -> None:
	history = make_history(5, total=Decimal("100.00"))
	invoice = make_invoice(total=Decimal("101.00"))

	flag = check_amount_outlier(invoice, history)

	assert flag is not None
	assert flag.rule == "amount_outlier"
	assert flag.reason.endswith(".")
	assert check_amount_outlier(invoice, history[:4]) is None


def test_new_vendor_rule_checks_history() -> None:
	invoice = make_invoice(vendor="New Supplier")

	flag = check_new_vendor(invoice, [])

	assert flag is not None
	assert flag.rule == "new_vendor"
	assert flag.reason.endswith(".")
	assert check_new_vendor(invoice, [make_invoice(vendor="new supplier")]) is None


def test_date_oddity_rule_flags_future_and_old_dates() -> None:
	future_invoice = make_invoice(invoice_date=date.today() + timedelta(days=1))
	old_invoice = make_invoice(invoice_date=date.today() - timedelta(days=366))

	assert check_date_oddity(future_invoice, []).rule == "date_oddity"
	assert check_date_oddity(old_invoice, []).rule == "date_oddity"
	assert check_date_oddity(make_invoice(), []) is None


def test_missing_fields_rule_lists_missing_values() -> None:
	invoice = make_invoice(vendor=None, invoice_number="", total=None)

	flag = check_missing_fields(invoice, [])

	assert flag is not None
	assert flag.rule == "missing_fields"
	assert "vendor, invoice number, total" in flag.reason
	assert flag.reason.endswith(".")


def test_run_all_rules_returns_flags_for_triggered_rules() -> None:
	invoice = make_invoice(
		vendor=None,
		invoice_number=None,
		invoice_date=date.today() + timedelta(days=1),
		total=None,
	)

	flags = run_all_rules(invoice, [])

	assert {flag.rule for flag in flags} == {"date_oddity", "missing_fields"}
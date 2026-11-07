from datetime import date, timedelta
from decimal import Decimal
from statistics import mean, pstdev
from typing import Callable

from app.models import Flag, Invoice

CENT = Decimal("0.01")


def _flag(invoice: Invoice, rule: str, severity: str, reason: str) -> Flag:
	return Flag(invoice=invoice, rule=rule, severity=severity, reason=reason)


def check_duplicate(invoice: Invoice, history: list[Invoice]) -> Flag | None:
	"""Flag an invoice that repeats a vendor and invoice number or total and date."""
	for previous in history:
		if previous is invoice or (invoice.id is not None and previous.id == invoice.id):
			continue
		same_vendor = bool(invoice.vendor and previous.vendor) and (
			invoice.vendor.strip().casefold() == previous.vendor.strip().casefold()
		)
		same_number = bool(invoice.invoice_number and previous.invoice_number) and (
			invoice.invoice_number.strip().casefold()
			== previous.invoice_number.strip().casefold()
		)
		same_amount_and_date = (
			invoice.total is not None
			and invoice.invoice_date is not None
			and invoice.total == previous.total
			and invoice.invoice_date == previous.invoice_date
		)
		if same_vendor and (same_number or same_amount_and_date):
			return _flag(
				invoice,
				"duplicate",
				"high",
				f"This invoice appears to duplicate an earlier invoice from {invoice.vendor}.",
			)
	return None


def check_math_mismatch(invoice: Invoice, history: list[Invoice]) -> Flag | None:
	"""Flag a total that differs from line totals plus tax by more than one cent."""
	if invoice.total is None:
		return None

	line_total = sum((item.line_total for item in invoice.line_items), Decimal("0"))
	expected_total = line_total + (invoice.tax_amount or Decimal("0"))
	if abs(expected_total - invoice.total) > CENT:
		return _flag(
			invoice,
			"math_mismatch",
			"high",
			f"The invoice total ({invoice.total}) does not match line items plus tax ({expected_total}).",
		)
	return None


def check_line_mismatch(invoice: Invoice, history: list[Invoice]) -> Flag | None:
	"""Flag the first line item whose quantity times price differs from its total."""
	for item in invoice.line_items:
		expected_total = item.quantity * item.unit_price
		if abs(expected_total - item.line_total) > CENT:
			return _flag(
				invoice,
				"line_mismatch",
				"medium",
				f"The line item '{item.description}' total does not match its quantity and unit price.",
			)
	return None


def check_amount_outlier(invoice: Invoice, history: list[Invoice]) -> Flag | None:
	"""Flag a vendor invoice above three population standard deviations from history."""
	if invoice.vendor is None or invoice.total is None:
		return None

	vendor_totals = [
		float(previous.total)
		for previous in history
		if previous is not invoice
		and previous.vendor is not None
		and previous.vendor.strip().casefold() == invoice.vendor.strip().casefold()
		and previous.total is not None
	]
	if len(vendor_totals) < 5:
		return None

	threshold = mean(vendor_totals) + 3 * pstdev(vendor_totals)
	if float(invoice.total) > threshold:
		return _flag(
			invoice,
			"amount_outlier",
			"medium",
			f"This invoice total is unusually high compared with {invoice.vendor}'s previous invoices.",
		)
	return None


def check_new_vendor(invoice: Invoice, history: list[Invoice]) -> Flag | None:
	"""Flag a vendor that does not appear in invoice history."""
	if invoice.vendor is None or not invoice.vendor.strip():
		return None

	vendor = invoice.vendor.strip().casefold()
	if not any(
		previous is not invoice
		and previous.vendor is not None
		and previous.vendor.strip().casefold() == vendor
		for previous in history
	):
		return _flag(
			invoice,
			"new_vendor",
			"low",
			f"This is the first recorded invoice from {invoice.vendor}.",
		)
	return None


def check_date_oddity(invoice: Invoice, history: list[Invoice]) -> Flag | None:
	"""Flag an invoice dated in the future or more than 365 days ago."""
	if invoice.invoice_date is None:
		return None

	today = date.today()
	if invoice.invoice_date > today:
		reason = "The invoice date is in the future."
	elif invoice.invoice_date < today - timedelta(days=365):
		reason = "The invoice date is more than 365 days old."
	else:
		return None
	return _flag(invoice, "date_oddity", "medium", reason)


def check_missing_fields(invoice: Invoice, history: list[Invoice]) -> Flag | None:
	"""Flag an invoice with no vendor, invoice number, or total."""
	missing_fields = []
	if invoice.vendor is None or not invoice.vendor.strip():
		missing_fields.append("vendor")
	if invoice.invoice_number is None or not invoice.invoice_number.strip():
		missing_fields.append("invoice number")
	if invoice.total is None:
		missing_fields.append("total")
	if not missing_fields:
		return None

	fields = ", ".join(missing_fields)
	return _flag(
		invoice,
		"missing_fields",
		"high",
		f"This invoice is missing required information: {fields}.",
	)


def run_all_rules(invoice: Invoice, history: list[Invoice]) -> list[Flag]:
	"""Run each anomaly rule and return the flags it produces."""
	rules: tuple[Callable[[Invoice, list[Invoice]], Flag | None], ...] = (
		check_duplicate,
		check_math_mismatch,
		check_line_mismatch,
		check_amount_outlier,
		check_new_vendor,
		check_date_oddity,
		check_missing_fields,
	)
	flags = []
	for rule in rules:
		flag = rule(invoice, history)
		if flag is not None:
			flags.append(flag)
	return flags
import csv
import io
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from openai import OpenAIError
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.anomalies import run_all_rules
from app.db import Base, engine, get_db
from app.extractor import ExtractionError, ExtractedInvoice, extract_invoice
from app.models import Flag, Invoice, LineItem

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
INVOICE_NOT_FOUND = "Invoice not found."
ALLOWED_MIME_TYPES = {
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".png": "image/png",
	".pdf": "application/pdf",
}
@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
	Base.metadata.create_all(bind=engine)
	yield


app = FastAPI(title="LedgerLens", lifespan=lifespan)


class ReviewRequest(BaseModel):
	decision: Literal["approve", "reject"]
	note: str | None = None


async def read_validated_upload(file: UploadFile) -> tuple[bytes, str]:
	"""Read an upload within the size limit and verify its type and signature."""
	filename = file.filename or ""
	extension = Path(filename).suffix.casefold()
	expected_mime_type = ALLOWED_MIME_TYPES.get(extension)
	if expected_mime_type is None:
		raise HTTPException(status_code=415, detail="Only JPG, PNG, and PDF files are allowed.")
	if file.content_type not in (None, "application/octet-stream", expected_mime_type):
		raise HTTPException(status_code=415, detail="The file extension and content type do not match.")

	contents = await file.read(MAX_UPLOAD_BYTES + 1)
	if len(contents) > MAX_UPLOAD_BYTES:
		raise HTTPException(status_code=413, detail="The uploaded file exceeds the 5 MB limit.")
	if not contents:
		raise HTTPException(status_code=400, detail="The uploaded file is empty.")

	signatures = {
		".jpg": contents.startswith(b"\xff\xd8\xff"),
		".jpeg": contents.startswith(b"\xff\xd8\xff"),
		".png": contents.startswith(b"\x89PNG\r\n\x1a\n"),
		".pdf": contents.startswith(b"%PDF-"),
	}
	if not signatures[extension]:
		raise HTTPException(status_code=415, detail="The uploaded file contents are not a valid supported type.")
	return contents, expected_mime_type


def build_invoice(extracted: ExtractedInvoice) -> Invoice:
	"""Map validated extraction data to the database models."""
	invoice = Invoice(
		vendor=extracted.vendor,
		invoice_number=extracted.invoice_number,
		invoice_date=extracted.date,
		subtotal=extracted.subtotal,
		tax_amount=extracted.tax_amount,
		total=extracted.total,
		currency=extracted.currency,
		status="pending_review",
	)
	invoice.line_items = [
		LineItem(
			description=item.description,
			quantity=item.quantity,
			unit_price=item.unit_price,
			line_total=item.total,
		)
		for item in extracted.line_items
	]
	return invoice


def build_failed_extraction_invoice(error: ExtractionError) -> tuple[Invoice, Flag]:
	"""Create a reviewable invoice and flag when extraction returns invalid data."""
	invoice = Invoice(status="pending_review")
	flag = Flag(
		rule="extraction_failed",
		severity="high",
		reason=f"Invoice extraction failed and requires manual review: {error}",
		resolved=False,
	)
	flag.invoice = invoice
	return invoice, flag


def serialize_invoice(invoice: Invoice) -> dict[str, object]:
	"""Return an invoice and its related records as an API response."""
	return {
		"id": invoice.id,
		"vendor": invoice.vendor,
		"invoice_number": invoice.invoice_number,
		"invoice_date": invoice.invoice_date,
		"subtotal": invoice.subtotal,
		"tax_amount": invoice.tax_amount,
		"total": invoice.total,
		"currency": invoice.currency,
		"file_path": invoice.file_path,
		"status": invoice.status,
		"created_at": invoice.created_at,
		"line_items": [
			{
				"id": item.id,
				"description": item.description,
				"quantity": item.quantity,
				"unit_price": item.unit_price,
				"line_total": item.line_total,
			}
			for item in invoice.line_items
		],
		"flags": [
			{
				"id": flag.id,
				"rule": flag.rule,
				"severity": flag.severity,
				"reason": flag.reason,
				"resolved": flag.resolved,
				"reviewer_note": flag.reviewer_note,
			}
			for flag in invoice.flags
		],
	}


def load_invoice(db: Session, invoice_id: int) -> Invoice | None:
	"""Load an invoice with its related records."""
	statement = (
		select(Invoice)
		.where(Invoice.id == invoice_id)
		.options(selectinload(Invoice.line_items), selectinload(Invoice.flags))
	)
	return db.scalar(statement)


def save_invoice(db: Session, invoice: Invoice, history: list[Invoice]) -> Invoice:
	"""Persist an invoice and all anomaly flags in one transaction."""
	try:
		db.add(invoice)
		db.flush()
		db.add_all(run_all_rules(invoice, history))
		db.commit()
	except SQLAlchemyError as error:
		db.rollback()
		raise HTTPException(status_code=500, detail="Could not save the invoice.") from error
	saved_invoice = load_invoice(db, invoice.id)
	if saved_invoice is None:
		raise RuntimeError("The saved invoice could not be reloaded.")
	return saved_invoice


@app.post(
	"/invoices",
	status_code=status.HTTP_201_CREATED,
	responses={
		400: {"description": "The uploaded file is empty."},
		413: {"description": "The uploaded file exceeds the 5 MB limit."},
		415: {"description": "The file type is unsupported or its contents do not match."},
		500: {"description": "The invoice could not be saved."},
		502: {"description": "The invoice extraction service is unavailable."},
	},
)
async def upload_invoice(
	file: Annotated[UploadFile, File()],
	db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
	"""Validate, extract, review, and store an uploaded invoice."""
	try:
		contents, mime_type = await read_validated_upload(file)
	finally:
		await file.close()

	history = list(db.scalars(select(Invoice)).all())
	try:
		extracted = await run_in_threadpool(extract_invoice, contents, mime_type)
	except ExtractionError as error:
		invoice, failure_flag = build_failed_extraction_invoice(error)
		invoice.file_path = file.filename
		failure_flag.invoice = invoice
		db.add(failure_flag)
		return serialize_invoice(save_invoice(db, invoice, history))
	except OpenAIError as error:
		raise HTTPException(
			status_code=502,
			detail="The invoice extraction service is unavailable.",
		) from error

	invoice = build_invoice(extracted)
	invoice.file_path = file.filename
	return serialize_invoice(save_invoice(db, invoice, history))


@app.get("/invoices")
def list_invoices(
	db: Annotated[Session, Depends(get_db)],
	status_filter: Annotated[
		Literal["pending_review", "approved", "rejected"] | None,
		Query(alias="status"),
	] = None,
	vendor: str | None = None,
	start_date: date | None = None,
	end_date: date | None = None,
) -> list[dict[str, object]]:
	"""List invoices with optional status, vendor, and date filters."""
	statement = select(Invoice).options(
		selectinload(Invoice.line_items), selectinload(Invoice.flags)
	)
	if status_filter is not None:
		statement = statement.where(Invoice.status == status_filter)
	if vendor:
		statement = statement.where(Invoice.vendor.ilike(f"%{vendor}%"))
	if start_date is not None:
		statement = statement.where(Invoice.invoice_date >= start_date)
	if end_date is not None:
		statement = statement.where(Invoice.invoice_date <= end_date)
	invoices = db.scalars(statement.order_by(Invoice.created_at.desc())).all()
	return [serialize_invoice(invoice) for invoice in invoices]


@app.get("/invoices/{invoice_id}", responses={404: {"description": "Invoice not found."}})
def get_invoice(
	invoice_id: int,
	db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
	"""Return an invoice with its line items and flags."""
	invoice = load_invoice(db, invoice_id)
	if invoice is None:
		raise HTTPException(status_code=404, detail=INVOICE_NOT_FOUND)
	return serialize_invoice(invoice)


@app.post(
	"/invoices/{invoice_id}/review",
	responses={
		404: {"description": "Invoice not found."},
		500: {"description": "The invoice review could not be saved."},
	},
)
def review_invoice(
	invoice_id: int,
	review: ReviewRequest,
	db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
	"""Approve or reject an invoice and resolve its current flags."""
	invoice = load_invoice(db, invoice_id)
	if invoice is None:
		raise HTTPException(status_code=404, detail=INVOICE_NOT_FOUND)
	invoice.status = "approved" if review.decision == "approve" else "rejected"
	for flag in invoice.flags:
		if not flag.resolved:
			flag.resolved = True
			flag.reviewer_note = review.note
	try:
		db.commit()
	except SQLAlchemyError as error:
		db.rollback()
		raise HTTPException(status_code=500, detail="Could not save the invoice review.") from error
	reviewed_invoice = load_invoice(db, invoice_id)
	if reviewed_invoice is None:
		raise HTTPException(status_code=404, detail=INVOICE_NOT_FOUND)
	return serialize_invoice(reviewed_invoice)


@app.get("/reports/summary")
def report_summary(db: Annotated[Session, Depends(get_db)]) -> dict[str, object]:
	"""Summarize invoice spend by vendor and month plus unresolved flag count."""
	invoices = db.scalars(select(Invoice).where(Invoice.total.is_not(None))).all()
	vendor_spend: defaultdict[str, Decimal] = defaultdict(Decimal)
	monthly_spend: defaultdict[str, Decimal] = defaultdict(Decimal)
	for invoice in invoices:
		vendor_spend[invoice.vendor or "Unknown"] += invoice.total
		if invoice.invoice_date is not None:
			monthly_spend[invoice.invoice_date.strftime("%Y-%m")] += invoice.total

	flagged_count = db.scalar(
		select(func.count(Flag.id)).where(Flag.resolved.is_(False))
	) or 0
	return {
		"spend_by_vendor": [
			{"vendor": vendor, "total": total}
			for vendor, total in sorted(vendor_spend.items())
		],
		"spend_by_month": [
			{"month": month, "total": total}
			for month, total in sorted(monthly_spend.items())
		],
		"flagged_count": flagged_count,
	}


@app.get("/reports/export.csv")
def export_invoices_csv(db: Annotated[Session, Depends(get_db)]) -> StreamingResponse:
	"""Export invoice data and flag counts as CSV."""
	invoices = db.scalars(
		select(Invoice).options(selectinload(Invoice.flags)).order_by(Invoice.id)
	).all()
	output = io.StringIO()
	writer = csv.writer(output)
	writer.writerow(
		["id", "vendor", "invoice_number", "invoice_date", "subtotal", "tax_amount", "total", "currency", "status", "flag_count"]
	)
	for invoice in invoices:
		writer.writerow(
			[
				invoice.id,
				invoice.vendor,
				invoice.invoice_number,
				invoice.invoice_date,
				invoice.subtotal,
				invoice.tax_amount,
				invoice.total,
				invoice.currency,
				invoice.status,
				len(invoice.flags),
			]
		)
	return StreamingResponse(
		iter([output.getvalue()]),
		media_type="text/csv",
		headers={"Content-Disposition": "attachment; filename=invoices.csv"},
	)
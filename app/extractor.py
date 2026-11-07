import base64
import json
from datetime import date as Date
from decimal import Decimal
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, ValidationError

from app.config import FIREWORKS_API_KEY, FIREWORKS_MODEL

FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"


class ExtractedLineItem(BaseModel):
	model_config = ConfigDict(extra="forbid")

	description: str
	quantity: Decimal
	unit_price: Decimal
	total: Decimal


class ExtractedInvoice(BaseModel):
	model_config = ConfigDict(extra="forbid")

	vendor: str
	invoice_number: str
	date: Date
	line_items: list[ExtractedLineItem]
	subtotal: Decimal
	tax_amount: Decimal
	total: Decimal
	currency: str


class ExtractionError(RuntimeError):
	"""Raised when invoice extraction cannot return validated data."""


def build_messages(image_bytes: bytes, mime_type: str = "image/jpeg") -> list[dict[str, Any]]:
	"""Build the JSON-only invoice extraction request for an image."""
	if not image_bytes:
		raise ValueError("Invoice image is empty.")

	encoded_image = base64.b64encode(image_bytes).decode("ascii")
	image_data_url = f"data:{mime_type};base64,{encoded_image}"
	prompt = (
		"Extract the invoice details from this image. Return JSON only, with exactly these keys: "
		"vendor, invoice_number, date (ISO YYYY-MM-DD), line_items, subtotal, tax_amount, total, "
		"currency. Each line_items entry must have exactly: description, quantity, unit_price, total. "
		"Use numeric values for amounts and quantities. Do not include markdown or commentary."
	)
	return [
		{
			"role": "user",
			"content": [
				{"type": "text", "text": prompt},
				{"type": "image_url", "image_url": {"url": image_data_url}},
			],
		}
	]


def call_model(messages: list[dict[str, Any]]) -> str:
	"""Call the Fireworks chat completions endpoint and return its text response."""
	if not FIREWORKS_API_KEY:
		raise ExtractionError("FIREWORKS_API_KEY is not set in the environment.")

	client = OpenAI(api_key=FIREWORKS_API_KEY, base_url=FIREWORKS_BASE_URL)
	response = client.chat.completions.create(
		model=FIREWORKS_MODEL,
		messages=messages,
		response_format={"type": "json_object"},
	)
	content = response.choices[0].message.content
	if not content:
		raise ExtractionError("Fireworks returned an empty extraction response.")
	return content


def parse_response(response_text: str) -> ExtractedInvoice:
	"""Strip optional code fences, decode JSON, and validate the invoice fields."""
	cleaned_response = response_text.strip()
	if cleaned_response.startswith("```"):
		lines = cleaned_response.splitlines()
		lines = lines[1:]
		if lines and lines[-1].strip() == "```":
			lines = lines[:-1]
		cleaned_response = "\n".join(lines).strip()

	response_data = json.loads(cleaned_response)
	return ExtractedInvoice.model_validate(response_data)


def extract_invoice(image_bytes: bytes, mime_type: str = "image/jpeg") -> ExtractedInvoice:
	"""Extract an invoice, retrying once if the model response is invalid."""
	messages = build_messages(image_bytes, mime_type)
	for attempt in range(2):
		response_text = call_model(messages)
		try:
			return parse_response(response_text)
		except (json.JSONDecodeError, ValidationError) as error:
			if attempt == 1:
				raise ExtractionError(
					"Fireworks returned invalid invoice JSON after one retry."
				) from error
			messages = [
				*messages,
				{"role": "assistant", "content": response_text},
				{
					"role": "user",
					"content": "The previous response was invalid. Return a corrected JSON object "
					"with the exact requested keys only.",
				},
			]

	raise ExtractionError("Fireworks invoice extraction failed.")
# ee/pocketpaw_ee/paw_bar/catalog_csv.py — read an owner's product CSV into an
# import preview (``parse_catalog_csv`` → ``CatalogImportPreview``, source "csv").
#
# Pure: bytes in, preview out, no I/O. The route (catalog_routes, POST
# …/catalog/import/csv) caps the upload at ``CSV_MAX_BYTES`` before calling it.
# Untrusted input, so: the stdlib ``csv`` module only, a product cap (the catalog
# cap), and every product through ``catalog_import._normalise``, the same cleaner
# the site importers use (HTML stripped, lengths capped, https images only).
#
# Columns are matched by header, case-insensitively, with "_" read as a space, and
# with aliases, so an export from a platform uploads as-is:
#   * generic: name|title|product, price, currency, image|image_url|photo,
#     url|link|page, description, in_stock|stock|available, id|sku;
#   * Shopify's product export: Handle, Title, Body (HTML), Variant Price,
#     Variant Inventory Qty, Image Src. Its extra rows per product (variants,
#     more images) carry only the Handle; they fold into the product: lowest
#     price, in stock when any variant is, the first image;
#   * WooCommerce's product export: ID, Type, SKU, Name, Short description,
#     Description, In stock?, Stock, Sale price, Regular price, Images. Ids
#     become ``woo:<ID>``, the id the Store API importer mints, so a CSV and a
#     site import of one store merge; ``variation`` rows are skipped (counted).
# When several columns alias one field, the first non-empty in alias order wins
# (a Woo sale price over its regular price).
#
# A row that cannot be a product is a warning with its line number
# (``line:<n>:no_name|no_price|duplicate_id``); the other rows still import.
# ``id`` defaults to ``csv:<slug(name)>``. Prices are decimals in the row's
# currency, parsed by the site importers' own ``catalog_import._price`` (a lone
# comma followed by one or two digits is a decimal comma) after currency symbols
# are dropped, and converted with ``pocketpaw.money.to_minor``.

"""Parse an owner's product CSV (generic, Shopify or WooCommerce export)."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from pocketpaw_ee.paw_bar.catalog_import import CatalogImportPreview, _normalise, _price, _Raw

CSV_MAX_BYTES = 2 * 1024 * 1024
_MAX_ROW_WARNINGS = 50
_SLUG_CHARS = 80

# Field → header aliases, in priority order (normalised: lower case, "_" as space).
_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "handle", "sku", "variant sku", "product id"),
    "name": ("name", "title", "product", "product name"),
    "price": ("price", "sale price", "variant price", "regular price"),
    "currency": ("currency", "currency code"),
    "image": ("image", "image url", "photo", "image src", "images", "variant image"),
    "url": ("url", "link", "page", "product url", "permalink"),
    "description": ("short description", "description", "body (html)", "body html"),
    "in_stock": ("in stock", "in stock?", "available", "stock", "variant inventory qty"),
    "type": ("type",),
}
_IN = frozenset({"1", "true", "yes", "y", "t", "instock", "in stock", "available"})
_OUT = frozenset(
    {"0", "false", "no", "n", "f", "outofstock", "out of stock", "sold out", "unavailable"}
)
_PRICE_JUNK_RE = re.compile(r"[^\d.,\-]")
_SLUG_RE = re.compile(r"[^\w]+", re.UNICODE)


def _header_key(value: str) -> str:
    return re.sub(r"[\s_]+", " ", value.replace("﻿", "").strip().lower())


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _dialect(text: str) -> Any:
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        return csv.excel


def _cell_price(value: str) -> Decimal | None:
    """A cell's price: currency symbols and spaces dropped, then the importers'
    own ``_price`` rule (decimal comma included)."""
    text = _PRICE_JUNK_RE.sub("", value or "")
    return _price(text) if text else None


def _stock(value: str) -> bool | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    if text in _IN:
        return True
    if text in _OUT:
        return False
    try:
        return Decimal(text) > 0
    except InvalidOperation:
        return None


def _slug(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-_")[:_SLUG_CHARS]
    if slug:
        return slug
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]  # noqa: S324 — an id, not a secret


class _Columns:
    """Which column(s) feed each field, from the header row."""

    def __init__(self, header: list[str]) -> None:
        index: dict[str, int] = {}
        for i, cell in enumerate(header):
            index.setdefault(_header_key(cell), i)
        self.by_field = {
            field: [index[a] for a in aliases if a in index] for field, aliases in _ALIASES.items()
        }
        self.woo = "regular price" in index and "id" in index
        self.shopify = "handle" in index and "variant price" in index

    def get(self, row: list[str], field: str) -> str:
        for i in self.by_field[field]:
            if i < len(row) and row[i].strip():
                return row[i].strip()
        return ""


def _failed(reason: str) -> CatalogImportPreview:
    return CatalogImportPreview(status="failed", reason=reason)


def parse_catalog_csv(data: bytes, *, max_items: int) -> CatalogImportPreview:
    """The products in an owner's CSV, as an import preview. Never raises.

    ``reason`` on a failure: ``csv_empty`` | ``csv_unreadable`` |
    ``csv_no_name_column`` | ``csv_no_price_column``."""
    text = _decode(data)
    if not text.strip():
        return _failed("csv_empty")
    reader = csv.reader(io.StringIO(text, newline=""), _dialect(text))
    try:
        header = next(reader)
        cols = _Columns(header)
        if not cols.by_field["name"] and not (cols.shopify and cols.by_field["id"]):
            return _failed("csv_no_name_column")
        if not cols.by_field["price"]:
            return _failed("csv_no_price_column")

        products: dict[str, dict[str, Any]] = {}
        row_warnings: list[str] = []
        variations = 0
        capped = False
        line = reader.line_num
        for row in reader:
            start, line = line + 1, reader.line_num
            if not any(cell.strip() for cell in row):
                continue
            if cols.woo and cols.get(row, "type").lower() == "variation":
                variations += 1
                continue
            name = cols.get(row, "name")
            raw_id = cols.get(row, "id")
            price = _cell_price(cols.get(row, "price"))
            stock = _stock(cols.get(row, "in_stock"))
            image = cols.get(row, "image").split(",", 1)[0].strip()
            if cols.woo and raw_id:
                key = f"woo:{raw_id}"
            elif raw_id:
                key = f"csv:{raw_id}"
            else:
                key = f"csv:{_slug(name)}" if name else ""

            existing = products.get(key) if key else None
            if existing is not None and not name and cols.shopify:
                # A Shopify variant / extra-image row folds into its product.
                if price is not None and (existing["price"] is None or price < existing["price"]):
                    existing["price"] = price
                if stock is not None:
                    existing["in_stock"] = bool(existing["in_stock"]) or stock
                existing["image"] = existing["image"] or image
                continue
            if not name:
                row_warnings.append(f"line:{start}:no_name")
                continue
            if existing is not None:
                row_warnings.append(f"line:{start}:duplicate_id")
                continue
            if price is None and not cols.shopify:
                row_warnings.append(f"line:{start}:no_price")
                continue
            if len(products) >= max_items:
                capped = True
                break
            url = cols.get(row, "url")
            if not url and cols.shopify and raw_id:
                url = f"/products/{raw_id}"
            products[key] = {
                "line": start,
                "id": key,
                "name": name,
                "price": price,
                "currency": cols.get(row, "currency"),
                "image": image,
                "url": url,
                "description": cols.get(row, "description"),
                "in_stock": stock,
            }
    except csv.Error:
        return _failed("csv_unreadable")

    raws: list[_Raw] = []
    for p in products.values():
        if p["price"] is None:  # a Shopify product none of whose rows had a price
            row_warnings.append(f"line:{p['line']}:no_price")
            continue
        raws.append(
            _Raw(
                id=p["id"],
                name=p["name"],
                price=p["price"],
                currency=p["currency"],
                image=p["image"],
                url=p["url"],
                description=p["description"],
                in_stock=p["in_stock"],
            )
        )
    parsed = _normalise(raws, keep_absolute_urls=True)
    items = parsed.items[:max_items]

    row_warnings.sort(key=lambda w: int(w.split(":")[1]))
    warnings = row_warnings[:_MAX_ROW_WARNINGS]
    if len(row_warnings) > _MAX_ROW_WARNINGS:
        warnings.append(f"more_row_warnings:{len(row_warnings) - _MAX_ROW_WARNINGS}")
    if parsed.skipped_no_price:
        warnings.append(f"skipped_no_price:{parsed.skipped_no_price}")
    if variations:
        warnings.append(f"skipped_variations:{variations}")
    if capped:
        warnings.append("row_cap_reached")
    if any(not item.currency for item in items):
        warnings.append("currency_unknown")
    status = "empty" if not items else ("partial" if row_warnings or capped else "ok")
    return CatalogImportPreview(
        status=status,
        source="csv" if items else "",
        items=items,
        total_found=len(items),
        warnings=warnings,
    )


__all__ = ["CSV_MAX_BYTES", "parse_catalog_csv"]

# tests/ee/sites/test_catalog_csv.py — an owner's product CSV read into an import
# preview (ee.pocketpaw_ee.paw_bar.catalog_csv.parse_catalog_csv). Pure: bytes in.
#
#   * generic headers with aliases, any case, "_" as a space, sniffed delimiter;
#   * Shopify's product export (variant rows folded into their product) and
#     WooCommerce's (``woo:<ID>`` ids, variation rows skipped, sale over regular);
#   * per-row warnings with line numbers, the product cap, prices per currency
#     exponent, decimal commas, and the shared normaliser's cleaning.

from __future__ import annotations

from pocketpaw_ee.paw_bar.catalog_csv import parse_catalog_csv


def _parse(text: str, max_items: int = 5000):
    return parse_catalog_csv(text.encode("utf-8"), max_items=max_items)


def test_generic_headers_and_aliases():
    preview = _parse(
        "Title,Price,Currency,Photo,Link,Description,Available,SKU\n"
        "Stoneware mug,12.50,usd,https://cdn.example/mug.jpg,/products/mug,"
        "<b>Holds</b> 350ml,yes,MUG-1\n"
        'Green tea,"1,299",JPY,http://cdn.example/tea.jpg,https://shop.example/tea,,0,\n'
    )
    assert (preview.status, preview.source, preview.reason) == ("ok", "csv", "")
    mug, tea = preview.items
    assert (mug.id, mug.name, mug.price_cents, mug.currency) == (
        "csv:MUG-1",
        "Stoneware mug",
        1250,
        "USD",
    )
    assert (mug.image_url, mug.url, mug.description, mug.in_stock) == (
        "https://cdn.example/mug.jpg",
        "/products/mug",
        "Holds 350ml",
        True,
    )
    # No id column value: csv:<slug(name)>. ¥1,299 has no minor unit. http image dropped.
    assert (tea.id, tea.price_cents, tea.image_url, tea.in_stock) == (
        "csv:green-tea",
        1299,
        "",
        False,
    )
    assert tea.url == "https://shop.example/tea"
    assert preview.total_found == 2


def test_semicolons_decimal_commas_and_three_decimal_currencies():
    preview = _parse("name;price;currency\nDates;1,25;EUR\nOud;12.500;KWD\nTea;€ 4,5;\n")
    assert [(i.name, i.price_cents, i.currency) for i in preview.items] == [
        ("Dates", 125, "EUR"),
        ("Oud", 12500, "KWD"),
        ("Tea", 450, ""),
    ]
    assert "currency_unknown" in preview.warnings


def test_bad_rows_are_warnings_with_line_numbers_and_the_rest_import():
    preview = _parse(
        "name,price\n"
        "Mug,5\n"
        ",3\n"  # line 3: no name
        "Kettle,free\n"  # line 4: no price
        '"Multi\nline",7\n'  # lines 5-6: a quoted newline is one row
        "Mug,6\n"  # line 7: same slug as line 2
        "\n"
        "Lamp,9\n"
    )
    assert [i.name for i in preview.items] == ["Mug", "Multi line", "Lamp"]
    assert preview.status == "partial"
    assert preview.warnings[:3] == ["line:3:no_name", "line:4:no_price", "line:7:duplicate_id"]


def test_the_product_cap():
    rows = "".join(f"P{i},{i}\n" for i in range(10))
    preview = _parse("name,price\n" + rows, max_items=4)
    assert len(preview.items) == 4
    assert "row_cap_reached" in preview.warnings


def test_shopify_export_folds_variant_rows():
    preview = _parse(
        "Handle,Title,Body (HTML),Vendor,Variant SKU,Variant Inventory Qty,"
        "Variant Price,Image Src\n"
        "mug,Stoneware mug,<p>Glazed</p>,Brew,MUG-S,0,14.00,https://cdn.shopify.com/a.jpg\n"
        "mug,,,,MUG-L,3,12.00,https://cdn.shopify.com/b.jpg\n"
        "kettle,Kettle,,Brew,K-1,0,40.00,\n"
        ",,,,,,,https://cdn.shopify.com/c.jpg\n"
    )
    mug, kettle = preview.items
    assert (mug.id, mug.price_cents, mug.in_stock, mug.url) == (
        "csv:mug",
        1200,
        True,
        "/products/mug",
    )
    assert (mug.image_url, mug.description) == ("https://cdn.shopify.com/a.jpg", "Glazed")
    assert (kettle.price_cents, kettle.in_stock) == (4000, False)
    assert "line:5:no_name" in preview.warnings


def test_woocommerce_export():
    preview = _parse(
        "ID,Type,SKU,Name,Published,Short description,Description,In stock?,Stock,"
        "Sale price,Regular price,Images\n"
        '41,variable,TEE,Tee,1,Soft cotton,Long text,1,,,20,"https://shop.example/a.jpg, https://shop.example/b.jpg"\n'
        "42,variation,TEE-R,Tee - Red,1,,,1,,,20,\n"
        "43,simple,CAP,Cap,1,,A cap,0,0,8,10,\n"
    )
    tee, cap = preview.items
    assert (tee.id, tee.price_cents, tee.description, tee.in_stock) == (
        "woo:41",
        2000,
        "Soft cotton",
        True,
    )
    assert tee.image_url == "https://shop.example/a.jpg"
    assert (cap.id, cap.price_cents, cap.description, cap.in_stock) == (
        "woo:43",
        800,
        "A cap",
        False,
    )
    assert "skipped_variations:1" in preview.warnings


def test_failures():
    assert _parse("").reason == "csv_empty"
    assert _parse("sku,price\nA,1\n").reason == "csv_no_name_column"
    assert _parse("name,colour\nA,red\n").reason == "csv_no_price_column"
    assert _parse("name,price\n").status == "empty"


def test_a_latin1_file_is_read():
    preview = parse_catalog_csv("name,price\nCafé,3\n".encode("cp1252"), max_items=10)
    assert preview.items[0].name == "Café"

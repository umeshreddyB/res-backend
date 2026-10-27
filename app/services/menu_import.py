import csv
import io
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import Category, MenuItem, OrderItem
from ..schemas import MenuImportReport, MenuImportRowError

MAX_FILE_SIZE_BYTES = 1024 * 1024  # 1 MB
MAX_ROWS = 2000

REQUIRED_FIELDS = ["category", "name", "price", "is_veg"]

HEADER_ALIASES = {
    "category": {"category", "category_name", "category name", "cat", "category_title"},
    "name": {"name", "item_name", "item name", "dish", "dish_name", "dish name", "item"},
    "price": {"price", "cost", "rate", "amount", "price (inr)", "price(inr)", "price inr", "price_inr", "price (rs)", "price(rs)", "price rs"},
    "is_veg": {"is_veg", "veg", "vegetarian", "is_vegetarian", "veg_non_veg", "veg/non-veg", "veg_or_non_veg", "type"},
    "description": {"description", "desc", "details"},
    "is_available": {"is_available", "available", "availability", "in_stock"},
    "image_url": {"image_url", "image", "photo", "img"},
}

VEG_VALUES = {"yes", "true", "1", "veg", "vegetarian", "y"}
NON_VEG_VALUES = {"no", "false", "0", "non-veg", "nonveg", "non_veg", "non veg", "n"}


def unescape_csv_formula(val: str) -> str:
    """If cell was saved with leading single-quote for formula safety, strip it."""
    if val.startswith("'") and len(val) > 1 and val[1] in ("=", "+", "-", "@"):
        return val[1:]
    return val


def escape_csv_formula(val: str) -> str:
    """Prefix cells starting with formula characters with a single quote to prevent CSV injection."""
    if val and val[0] in ("=", "+", "-", "@"):
        return f"'{val}"
    return val


def parse_price(raw: str) -> Decimal:
    """Parses a price string, accepting rupee symbol ₹, Rs, thousands commas.

    Rejects zero, negative, non-numeric, and more than 2 decimal places.
    """
    cleaned = unescape_csv_formula(raw.strip())
    # Remove rupee symbol
    cleaned = cleaned.replace("₹", "").strip()
    # Remove Rs / Rs. prefix case-insensitively
    cleaned = re.sub(r"^(rs\.?|inr)\s*", "", cleaned, flags=re.IGNORECASE).strip()
    # Remove thousands separators
    cleaned = cleaned.replace(",", "")

    if not cleaned:
        raise ValueError("Price cannot be empty")

    try:
        val = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"Invalid price '{raw}': must be a valid number")

    if val.is_nan() or val.is_infinite():
        raise ValueError("Invalid price: must be a finite number")

    if val <= Decimal("0"):
        raise ValueError("Price must be greater than zero")

    # Check decimal places
    if abs(val.as_tuple().exponent) > 2:
        raise ValueError("Price cannot have more than 2 decimal places")

    return val.quantize(Decimal("0.01"))


def parse_veg_flag(raw: str) -> bool:
    """Parses required is_veg column.

    Accepts yes/no/true/false/1/0/veg/non-veg.
    """
    cleaned = unescape_csv_formula(raw.strip().lower())
    if cleaned in VEG_VALUES:
        return True
    if cleaned in NON_VEG_VALUES:
        return False
    raise ValueError(
        "is_veg is required and must be one of: yes, no, true, false, 1, 0, veg, non-veg"
    )


def parse_availability_flag(raw: str) -> bool:
    """Parses optional is_available column. Defaults to True if empty."""
    cleaned = unescape_csv_formula(raw.strip().lower())
    if not cleaned:
        return True
    if cleaned in VEG_VALUES or cleaned == "available":
        return True
    if cleaned in NON_VEG_VALUES or cleaned == "unavailable":
        return False
    raise ValueError(
        "is_available must be one of: yes, no, true, false, 1, 0, available, unavailable"
    )


def detect_delimiter(sample_line: str) -> str:
    """Detects whether delimiter is comma, semicolon, or tab based on header match."""
    candidates = [",", ";", "\t"]
    best_delim = ","
    best_matches = -1

    for delim in candidates:
        tokens = [t.strip().strip("'\"").lower() for t in sample_line.split(delim)]
        matches = 0
        for token in tokens:
            for canonical, aliases in HEADER_ALIASES.items():
                if token in aliases:
                    matches += 1
                    break
        if matches > best_matches:
            best_matches = matches
            best_delim = delim

    return best_delim


def import_menu_csv(
    db: Session,
    file_bytes: bytes,
    dry_run: bool = True,
    mode: str = "merge",
) -> MenuImportReport:
    """Parses, validates, and imports a menu CSV in 'merge' or 'replace' mode.

    All-or-nothing: if any row fails validation, nothing is saved.
    """
    # 1. Size check
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        return MenuImportReport(
            dry_run=dry_run,
            mode=mode,
            committed=False,
            rows_read=0,
            categories_created=0,
            items_created=0,
            items_updated=0,
            items_unchanged=0,
            items_deleted=0,
            categories_deleted=0,
            errors=[MenuImportRowError(row=1, field="file", message="File exceeds maximum size of 1MB")],
        )

    # 2. Encoding check (accept UTF-8 with or without BOM)
    try:
        # utf-8-sig automatically strips BOM if present
        text_content = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return MenuImportReport(
            dry_run=dry_run,
            mode=mode,
            committed=False,
            rows_read=0,
            categories_created=0,
            items_created=0,
            items_updated=0,
            items_unchanged=0,
            items_deleted=0,
            categories_deleted=0,
            errors=[MenuImportRowError(row=1, field="file", message="Invalid file encoding. File must be UTF-8 encoded.")],
        )

    lines = [line for line in text_content.splitlines() if line.strip()]
    if not lines:
        return MenuImportReport(
            dry_run=dry_run,
            mode=mode,
            committed=False,
            rows_read=0,
            categories_created=0,
            items_created=0,
            items_updated=0,
            items_unchanged=0,
            items_deleted=0,
            categories_deleted=0,
            errors=[MenuImportRowError(row=1, field="file", message="File is empty")],
        )

    delimiter = detect_delimiter(lines[0])
    reader = csv.reader(lines, delimiter=delimiter)

    try:
        header_raw = next(reader)
    except StopIteration:
        return MenuImportReport(
            dry_run=dry_run,
            mode=mode,
            committed=False,
            rows_read=0,
            categories_created=0,
            items_created=0,
            items_updated=0,
            items_unchanged=0,
            items_deleted=0,
            categories_deleted=0,
            errors=[MenuImportRowError(row=1, field="file", message="CSV file has no header")],
        )

    # Map header indices to canonical column names
    col_map: dict[str, int] = {}
    for idx, col in enumerate(header_raw):
        clean_col = unescape_csv_formula(col.strip().strip("'\"").lower())
        for canonical, aliases in HEADER_ALIASES.items():
            if clean_col in aliases:
                col_map[canonical] = idx
                break

    # Check required fields
    missing_required = [req for req in REQUIRED_FIELDS if req not in col_map]
    if missing_required:
        return MenuImportReport(
            dry_run=dry_run,
            mode=mode,
            committed=False,
            rows_read=0,
            categories_created=0,
            items_created=0,
            items_updated=0,
            items_unchanged=0,
            items_deleted=0,
            categories_deleted=0,
            errors=[
                MenuImportRowError(
                    row=1,
                    field="header",
                    message=f"Missing required column(s): {', '.join(missing_required)}",
                )
            ],
        )

    # Parse and validate data rows
    errors: list[MenuImportRowError] = []
    parsed_rows: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()

    rows_read = 0
    for row_idx, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue  # skip completely blank lines

        rows_read += 1
        if rows_read > MAX_ROWS:
            errors.append(MenuImportRowError(row=row_idx, field="file", message=f"File exceeds maximum of {MAX_ROWS} rows"))
            break

        # Helper to get cell value safely
        def get_val(canonical: str) -> str:
            idx = col_map.get(canonical)
            if idx is not None and idx < len(row):
                return row[idx].strip()
            return ""

        cat_raw = unescape_csv_formula(get_val("category"))
        name_raw = unescape_csv_formula(get_val("name"))
        price_raw = get_val("price")
        veg_raw = get_val("is_veg")
        desc_raw = unescape_csv_formula(get_val("description"))
        avail_raw = get_val("is_available")
        img_raw = unescape_csv_formula(get_val("image_url"))

        row_has_error = False

        # Category validation
        if not cat_raw:
            errors.append(MenuImportRowError(row=row_idx, field="category", message="Category cannot be empty"))
            row_has_error = True
        elif len(cat_raw) > 100:
            errors.append(MenuImportRowError(row=row_idx, field="category", message="Category name exceeds 100 characters"))
            row_has_error = True

        # Name validation
        if not name_raw:
            errors.append(MenuImportRowError(row=row_idx, field="name", message="Item name cannot be empty"))
            row_has_error = True
        elif len(name_raw) > 150:
            errors.append(MenuImportRowError(row=row_idx, field="name", message="Item name exceeds 150 characters"))
            row_has_error = True

        # Duplicate check inside file
        if cat_raw and name_raw:
            key = (cat_raw.lower(), name_raw.lower())
            if key in seen_keys:
                errors.append(
                    MenuImportRowError(
                        row=row_idx,
                        field="name",
                        message=f"Duplicate item '{name_raw}' in category '{cat_raw}' inside file",
                    )
                )
                row_has_error = True
            else:
                seen_keys.add(key)

        # Price validation
        parsed_price = None
        if not price_raw:
            errors.append(MenuImportRowError(row=row_idx, field="price", message="Price cannot be empty"))
            row_has_error = True
        else:
            try:
                parsed_price = parse_price(price_raw)
            except ValueError as e:
                errors.append(MenuImportRowError(row=row_idx, field="price", message=str(e)))
                row_has_error = True

        # is_veg validation
        parsed_veg = None
        if not veg_raw:
            errors.append(
                MenuImportRowError(
                    row=row_idx,
                    field="is_veg",
                    message="is_veg is required and must be yes/no/true/false/1/0/veg/non-veg",
                )
            )
            row_has_error = True
        else:
            try:
                parsed_veg = parse_veg_flag(veg_raw)
            except ValueError as e:
                errors.append(MenuImportRowError(row=row_idx, field="is_veg", message=str(e)))
                row_has_error = True

        # is_available validation
        parsed_avail = True
        if avail_raw:
            try:
                parsed_avail = parse_availability_flag(avail_raw)
            except ValueError as e:
                errors.append(MenuImportRowError(row=row_idx, field="is_available", message=str(e)))
                row_has_error = True

        if not row_has_error:
            parsed_rows.append({
                "row_num": row_idx,
                "category": cat_raw,
                "name": name_raw,
                "price": parsed_price,
                "is_veg": parsed_veg,
                "is_available": parsed_avail,
                "description": desc_raw if desc_raw else None,
                "image_url": img_raw if img_raw else None,
            })

    # Protection: refuse replace if file has no valid rows
    if mode == "replace" and rows_read == 0:
        errors.append(MenuImportRowError(row=1, field="file", message="Refusing to replace menu with empty file"))

    # All-or-nothing: if any errors exist, do nothing and return report
    if errors:
        return MenuImportReport(
            dry_run=dry_run,
            mode=mode,
            committed=False,
            rows_read=rows_read,
            categories_created=0,
            items_created=0,
            items_updated=0,
            items_unchanged=0,
            items_deleted=0,
            categories_deleted=0,
            errors=errors,
        )

    if mode == "replace" and len(parsed_rows) == 0:
        return MenuImportReport(
            dry_run=dry_run,
            mode=mode,
            committed=False,
            rows_read=rows_read,
            categories_created=0,
            items_created=0,
            items_updated=0,
            items_unchanged=0,
            items_deleted=0,
            categories_deleted=0,
            errors=[MenuImportRowError(row=1, field="file", message="Refusing to replace menu when file has no valid rows")],
        )

    # Database reconciliation
    existing_categories = {c.name.strip().lower(): c for c in db.scalars(select(Category)).all()}
    existing_items = {
        (item.category.name.strip().lower(), item.name.strip().lower()): item
        for item in db.scalars(select(MenuItem)).all()
    }

    categories_created = 0
    items_created = 0
    items_updated = 0
    items_unchanged = 0
    items_deleted = 0
    categories_deleted = 0

    incoming_keys = set()
    incoming_cat_names = set()

    for r in parsed_rows:
        cat_key = r["category"].strip().lower()
        incoming_cat_names.add(cat_key)
        item_key = (cat_key, r["name"].strip().lower())
        incoming_keys.add(item_key)

        # Category handling
        cat = existing_categories.get(cat_key)
        if not cat:
            categories_created += 1
            if not dry_run:
                cat = Category(name=r["category"].strip())
                db.add(cat)
                db.flush()
                existing_categories[cat_key] = cat

        # Item handling
        existing_item = existing_items.get(item_key)
        if existing_item:
            # Check if any field changed
            changed = False
            if existing_item.price != r["price"]:
                changed = True
            if existing_item.is_veg != r["is_veg"]:
                changed = True
            if existing_item.is_available != r["is_available"]:
                changed = True
            # In merge mode, blank description/image keeps existing value
            if r["description"] is not None and existing_item.description != r["description"]:
                changed = True
            if r["image_url"] is not None and existing_item.image_url != r["image_url"]:
                changed = True

            if changed:
                items_updated += 1
                if not dry_run:
                    existing_item.price = r["price"]
                    existing_item.is_veg = r["is_veg"]
                    existing_item.is_available = r["is_available"]
                    if r["description"] is not None:
                        existing_item.description = r["description"]
                    if r["image_url"] is not None:
                        existing_item.image_url = r["image_url"]
            else:
                items_unchanged += 1
        else:
            items_created += 1
            if not dry_run:
                new_item = MenuItem(
                    category_id=cat.id,
                    name=r["name"].strip(),
                    price=r["price"],
                    is_veg=r["is_veg"],
                    is_available=r["is_available"],
                    description=r["description"],
                    image_url=r["image_url"],
                )
                db.add(new_item)
                db.flush()
                existing_items[item_key] = new_item

    # Replace mode: delete items and categories not in incoming file
    if mode == "replace":
        items_to_delete = [
            item for key, item in existing_items.items()
            if key not in incoming_keys
        ]
        items_deleted = len(items_to_delete)

        if items_deleted > 0 and not dry_run:
            item_ids = [item.id for item in items_to_delete]
            # Disassociate from order_items (past orders keep their saved item names and snapshots)
            db.execute(
                update(OrderItem)
                .where(OrderItem.menu_item_id.in_(item_ids))
                .values(menu_item_id=None)
            )
            for item in items_to_delete:
                db.delete(item)
            db.flush()

        # Delete categories not in file and having no items
        categories_to_delete = [
            c for key, c in existing_categories.items()
            if key not in incoming_cat_names
        ]
        categories_deleted = len(categories_to_delete)

        if categories_deleted > 0 and not dry_run:
            for c in categories_to_delete:
                db.delete(c)
            db.flush()

    if not dry_run:
        db.commit()

    return MenuImportReport(
        dry_run=dry_run,
        mode=mode,
        committed=(not dry_run),
        rows_read=rows_read,
        categories_created=categories_created,
        items_created=items_created,
        items_updated=items_updated,
        items_unchanged=items_unchanged,
        items_deleted=items_deleted,
        categories_deleted=categories_deleted,
        errors=[],
    )


def export_menu_csv(db: Session) -> str:
    """Exports current menu as CSV with exact same column headers for round-trip compatibility."""
    categories = db.scalars(select(Category).order_by(Category.sort_order, Category.name)).all()

    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")

    # Header
    writer.writerow(["category", "name", "price", "is_veg", "is_available", "description", "image_url"])

    for cat in categories:
        items = db.scalars(
            select(MenuItem)
            .where(MenuItem.category_id == cat.id)
            .order_by(MenuItem.name)
        ).all()
        for item in items:
            writer.writerow([
                escape_csv_formula(cat.name),
                escape_csv_formula(item.name),
                f"{item.price:.2f}",
                "yes" if item.is_veg else "no",
                "yes" if item.is_available else "no",
                escape_csv_formula(item.description or ""),
                escape_csv_formula(item.image_url or ""),
            ])

    return output.getvalue()

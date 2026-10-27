
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from ..core.config import settings
from ..db import get_db
from ..deps import require_admin
from ..models import Category, MenuItem, OrderItem
from ..schemas import (AvailabilityIn, CategoryIn, CategoryOut, CategoryUpdate,
                       CategoryWithItems, ItemIn, ItemOut, ItemUpdate, MenuImportReport)
from ..services.menu_import import export_menu_csv, import_menu_csv
from ..services.storage import detect_image_type, storage

router = APIRouter(prefix="/menu", tags=["menu"])
admin_router = APIRouter(prefix="/admin", tags=["admin: menu"], dependencies=[Depends(require_admin)])


@router.get("", response_model=list[CategoryWithItems])
def get_menu(only_available: bool = Query(False, description="Hide items that are currently unavailable"),
             db: Session = Depends(get_db)):
    cats = db.scalars(select(Category).options(selectinload(Category.items))
                      .order_by(Category.sort_order, Category.name)).all()
    out = []
    for c in cats:
        items = [i for i in c.items if i.is_available or not only_available]
        out.append(CategoryWithItems(id=c.id, name=c.name, sort_order=c.sort_order,
                                     items=[ItemOut.model_validate(i) for i in items]))
    return out


@router.get("/items/{item_id}", response_model=ItemOut)
def get_item(item_id: int, db: Session = Depends(get_db)):
    item = db.get(MenuItem, item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    return item


# ---------------- admin ----------------
@admin_router.get("/categories", response_model=list[CategoryOut])
def list_categories(db: Session = Depends(get_db)):
    return db.scalars(select(Category).order_by(Category.sort_order, Category.name)).all()


@admin_router.post("/categories", response_model=CategoryOut, status_code=201)
def create_category(data: CategoryIn, db: Session = Depends(get_db)):
    if db.scalar(select(Category).where(Category.name == data.name)):
        raise HTTPException(409, "Category already exists")
    cat = Category(**data.model_dump())
    db.add(cat)
    db.commit()
    return cat


@admin_router.patch("/categories/{category_id}", response_model=CategoryOut)
def update_category(category_id: int, data: CategoryUpdate, db: Session = Depends(get_db)):
    cat = db.get(Category, category_id)
    if not cat:
        raise HTTPException(404, "Category not found")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(cat, k, v)
    db.commit()
    return cat


@admin_router.delete("/categories/{category_id}", status_code=204)
def delete_category(category_id: int, db: Session = Depends(get_db)):
    cat = db.get(Category, category_id)
    if not cat:
        raise HTTPException(404, "Category not found")
    item_ids = db.scalars(select(MenuItem.id).where(MenuItem.category_id == category_id)).all()
    if item_ids:
        db.execute(update(OrderItem).where(OrderItem.menu_item_id.in_(item_ids)).values(menu_item_id=None))
        for item in cat.items:
            if item.image_url:
                storage.delete(item.image_url)
    db.delete(cat)  # also deletes its items; past orders keep their item-name snapshots
    db.commit()


@admin_router.post("/items", response_model=ItemOut, status_code=201)
def create_item(data: ItemIn, db: Session = Depends(get_db)):
    if not db.get(Category, data.category_id):
        raise HTTPException(404, "Category not found")
    item = MenuItem(**data.model_dump())
    db.add(item)
    db.commit()
    return item


@admin_router.patch("/items/{item_id}", response_model=ItemOut)
def update_item(item_id: int, data: ItemUpdate, db: Session = Depends(get_db)):
    item = db.get(MenuItem, item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    changes = data.model_dump(exclude_unset=True)
    if "category_id" in changes and not db.get(Category, changes["category_id"]):
        raise HTTPException(404, "Category not found")
    for k, v in changes.items():
        setattr(item, k, v)
    db.commit()
    return item


@admin_router.patch("/items/{item_id}/availability", response_model=ItemOut)
def set_item_availability(item_id: int, data: AvailabilityIn, db: Session = Depends(get_db)):
    """One-tap 'sold out' / 'back in stock' switch for the admin screen."""
    item = db.get(MenuItem, item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    item.is_available = data.is_available
    db.commit()
    return item


@admin_router.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int, db: Session = Depends(get_db)):
    item = db.get(MenuItem, item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    if item.image_url:
        storage.delete(item.image_url)
    db.execute(update(OrderItem).where(OrderItem.menu_item_id == item_id).values(menu_item_id=None))
    db.delete(item)
    db.commit()


@admin_router.post("/items/{item_id}/image", response_model=ItemOut)
async def upload_item_image(item_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload an image for a menu item.

    Validates file content using magic bytes (JPEG, PNG, WebP, GIF).
    Enforces maximum upload size and saves under a secure random filename.
    """
    item = db.get(MenuItem, item_id)
    if not item:
        raise HTTPException(404, "Item not found")

    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    content = await file.read()
    if len(content) > max_bytes:
        raise HTTPException(413, f"Image file exceeds maximum allowed size of {settings.max_upload_size_mb}MB")

    # Validate file by real content (magic bytes), not trusting file extension or Content-Type header
    extension = detect_image_type(content)

    # Delete previous image if one was uploaded
    if item.image_url:
        storage.delete(item.image_url)

    # Save to storage (generates unique UUID filename)
    image_url = storage.save(content, extension)
    item.image_url = image_url
    db.commit()
    db.refresh(item)
    return item


@admin_router.delete("/items/{item_id}/image", response_model=ItemOut)
def delete_item_image(item_id: int, db: Session = Depends(get_db)):
    """Remove the image from a menu item and delete it from storage."""
    item = db.get(MenuItem, item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    if item.image_url:
        storage.delete(item.image_url)
        item.image_url = None
        db.commit()
        db.refresh(item)
    return item


@admin_router.post("/menu/import", response_model=MenuImportReport)
async def import_menu(
    file: UploadFile = File(...),
    dry_run: bool = Query(True, description="When true, validates and previews changes without saving"),
    mode: str = Query("merge", pattern="^(merge|replace)$", description="Import mode: merge or replace"),
    db: Session = Depends(get_db),
):
    """Import menu categories and items from a CSV file.

    Query parameters:
    - dry_run (default: true): Preview counts and validate without saving.
    - mode (default: merge): 'merge' adds/updates items; 'replace' also deletes items/categories not present in the CSV.
    """
    content = await file.read()
    return import_menu_csv(db, content, dry_run=dry_run, mode=mode)


@admin_router.get("/menu/export.csv")
def export_menu(db: Session = Depends(get_db)):
    """Download current menu as a CSV file matching the import format."""
    csv_data = export_menu_csv(db)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="menu_export.csv"'},
    )



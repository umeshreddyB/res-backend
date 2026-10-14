
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..deps import require_admin
from ..models import Category, MenuItem
from ..schemas import (AvailabilityIn, CategoryIn, CategoryOut, CategoryUpdate,
                       CategoryWithItems, ItemIn, ItemOut, ItemUpdate)

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
    db.delete(item)
    db.commit()

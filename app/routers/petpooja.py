from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_admin
from ..services.petpooja import (handle_menu_sync,
                                 handle_order_status_callback, push_order,
                                 retry_failed_pushes)

router = APIRouter(prefix="/petpooja", tags=["petpooja"])
admin_router = APIRouter(prefix="/admin/petpooja", tags=["admin: petpooja"],
                         dependencies=[Depends(require_admin)])


@router.post("/callback/order-status")
async def petpooja_order_status_callback(request: Request, db: Session = Depends(get_db)):
    """Webhook callback endpoint for PetPooja to report POS status changes (e.g. accepted, ready)."""
    payload = await request.json()
    result = handle_order_status_callback(db, payload)
    return result


@router.post("/callback/menu-sync")
async def petpooja_menu_sync_callback(request: Request, db: Session = Depends(get_db)):
    """Webhook callback endpoint for PetPooja to trigger menu synchronization."""
    payload = await request.json()
    result = handle_menu_sync(db, payload)
    return result


@admin_router.post("/retry")
def retry_petpooja_pushes():
    """Admin-only: trigger retry for all failed PetPooja order pushes."""
    count = retry_failed_pushes()
    return {"status": "ok", "retried_success_count": count}


@admin_router.post("/orders/{order_id}/push")
def manual_petpooja_push(order_id: int):
    """Admin-only: manually push an order to PetPooja."""
    success = push_order(order_id)
    if not success:
        raise HTTPException(400, "PetPooja push failed or PetPooja is disabled")
    return {"status": "ok", "order_id": order_id}

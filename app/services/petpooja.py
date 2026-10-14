"""PetPooja integration (stub).

Orders are pushed to PetPooja once they are confirmed (status 'placed'), so billing and the kitchen
ticket happen on the POS the restaurant already uses.

TODO once the client gives API access (app key, app secret, access token, restaurant id from PetPooja):
  1. Add the credentials to Settings / .env
  2. Map our order (items -> petpooja_item_id, customer, payment type prepaid/COD, totals) to their save-order payload
  3. Send it, store the returned id in Order.petpooja_order_id, and retry on failure
  4. Expose a callback endpoint for order-status updates (accepted / ready) and menu sync
Use the official PetPooja API document they share - do not guess the payload.
"""
import logging

from ..core.config import settings
from ..db import SessionLocal
from ..models import Order

log = logging.getLogger(__name__)


def push_order(order_id: int) -> None:
    if not settings.petpooja_enabled:
        log.info("PetPooja disabled; skipping push for order %s", order_id)
        return
    with SessionLocal() as db:
        order = db.get(Order, order_id)
        if not order:
            return
        log.warning("PetPooja push for order %s not implemented yet", order_id)

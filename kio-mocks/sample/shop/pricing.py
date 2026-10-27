"""Order pricing with volume discounts."""

DISCOUNT_TIERS = ((10, 0.10), (5, 0.05))


def discount_for(quantity):
    """The discount for buying `quantity` of one item."""
    for threshold, rate in DISCOUNT_TIERS:
        if quantity >= threshold:
            return rate
    return 0.0


def order_total(items):
    """The total of `(unit_price, quantity)` pairs, discounts applied."""
    subtotal = 0.0
    for unit_price, quantity in items:
        subtotal += unit_price * quantity * (1 - discount_for(quantity))
    return round(subtotal, 2)


def average_item_price(items):
    """What one item costs on average, discounts applied."""
    total_quantity = sum(quantity for _, quantity in items)
    return round(order_total(items) / total_quantity, 2)

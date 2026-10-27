from shop.pricing import average_item_price, discount_for, order_total


def test_a_single_item_has_no_discount():
    assert order_total([(10.0, 1)]) == 10.0


def test_five_of_an_item_earns_five_percent():
    assert discount_for(5) == 0.05


def test_ten_of_an_item_earns_ten_percent():
    assert order_total([(2.0, 10)]) == 18.0


def test_the_average_price_of_a_mixed_cart():
    assert average_item_price([(10.0, 1), (20.0, 1)]) == 15.0


def test_an_empty_cart_has_no_average_price():
    assert average_item_price([]) == 0.0

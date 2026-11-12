import pytest
from shop.cart import Cart


def test_add_same_item_twice_adds_quantities():
    c = Cart(); c.add("tea", 2.5, 2); c.add("tea", 2.5, 3)
    assert c.subtotal() == 12.5


def test_save10():
    c = Cart(); c.add("mug", 20, 1)
    assert c.total("save10") == 18.0


def test_half_applies_at_exactly_100():
    c = Cart(); c.add("kettle", 100, 1)
    assert c.total("HALF") == 50.0


def test_half_not_below_100():
    c = Cart(); c.add("cup", 99, 1)
    assert c.total("HALF") == 99


def test_bad_qty():
    with pytest.raises(ValueError):
        Cart().add("x", 1, 0)

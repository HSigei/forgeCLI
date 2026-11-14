import time
from cache import Cache


def test_set_get():
    c = Cache(); c.set("a", 1)
    assert c.get("a") == 1


def test_ttl_expires():
    c = Cache(); c.set("a", 1, ttl=0.2)
    assert c.get("a") == 1
    time.sleep(0.3)
    assert c.get("a") is None


def test_max_size_evicts_least_recently_used():
    c = Cache(max_size=2)
    c.set("a", 1); c.set("b", 2)
    c.get("a")
    c.set("c", 3)
    assert c.get("b") is None and c.get("a") == 1 and c.get("c") == 3


def test_len_skips_expired():
    c = Cache(); c.set("a", 1, ttl=0.1); c.set("b", 2)
    time.sleep(0.2)
    assert len(c) == 1

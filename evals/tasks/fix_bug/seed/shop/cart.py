from .pricing import discount_for


class Cart:
    def __init__(self):
        self.items = []  # (name, unit_price, qty)

    def add(self, name, unit_price, qty=1):
        if qty <= 0:
            raise ValueError("qty must be positive")
        for i, (n, p, q) in enumerate(self.items):
            if n == name:
                self.items[i] = (n, p, qty)
                return
        self.items.append((name, unit_price, qty))

    def subtotal(self):
        return sum(p * q for _, p, q in self.items)

    def total(self, code=None):
        sub = self.subtotal()
        return round(sub - sub * discount_for(code, sub), 2)

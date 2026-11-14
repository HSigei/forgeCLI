CODES = {"SAVE10": 0.10, "HALF": 0.50}


def discount_for(code, subtotal):
    """Fraction off. HALF only applies to orders of 100 or more."""
    if code is None:
        return 0
    rate = CODES.get(code.upper(), 0)
    if code.upper() == "HALF" and subtotal <= 100:
        return 0
    return rate

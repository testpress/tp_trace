from django import template

register = template.Library()


@register.filter(name="currency")
def currency(value, symbol="$"):
    try:
        return f"{symbol}{float(value):,.2f}"
    except (TypeError, ValueError):
        return value


@register.filter(name="stock_status")
def stock_status(value):
    stock = getattr(value, "stock", value)
    try:
        stock = int(stock)
    except (TypeError, ValueError):
        return "unknown"
    if stock <= 0:
        return "out"
    if stock < 10:
        return "low"
    return "in-stock"


@register.simple_tag
def throw_template_error(message="Deliberate runtime error inside template partial"):
    """Template tag to simulate a deliberate runtime crash during template rendering."""
    raise ValueError(message)

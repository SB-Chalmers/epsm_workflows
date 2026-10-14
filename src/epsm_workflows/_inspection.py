"""Bounded formatting of already loaded, scalar inspection fields."""


def inspection(class_name, **fields):
    """Keep final object text on one line and within the handbook's 160 characters."""
    parts = []
    for key, value in fields.items():
        rendered = repr(value)
        if len(rendered) > 32:
            text = str(value)[:24] + "…"
            while len(repr(text)) > 32:
                text = text[:-2] + "…"
            rendered = repr(text)
        parts.append(f"{key}={rendered}")
    name = class_name[:40]
    while len(f"{name}({', '.join(parts)})") > 160:
        parts.pop()
    return f"{name}({', '.join(parts)})"

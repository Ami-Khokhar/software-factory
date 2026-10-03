import re
import unicodedata


def slugify(text, max_length=60):
    if max_length < 1:
        raise ValueError("max_length must be 1 or more")
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    slug = "-".join(re.findall(r"[a-z0-9]+", plain.lower()))
    if len(slug) <= max_length:
        return slug
    cut = slug.rfind("-", 0, max_length + 1)
    return slug[:cut] if cut > 0 else slug[:max_length]

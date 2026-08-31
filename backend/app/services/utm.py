from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def build_pinterest_utm_url(
    destination_url: str,
    *, campaign: str,
    content: str,
    source: str = "pinterest",
    medium: str = "organic_social",
) -> str:
    parts = urlsplit(destination_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({
        "utm_source": source,
        "utm_medium": medium,
        "utm_campaign": campaign,
        "utm_content": content,
    })
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

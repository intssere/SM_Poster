BOARD_ALIASES = {
    "womens-fragrance": "Women's Fragrance",
    "mens-fragrance": "Men's Fragrance",
    "arabian-fragrance": "Arabian Fragrance",
    "designer-fragrances": "Designer Fragrances",
    "niche-fragrances": "Niche Fragrances",
    "gift-sets": "Gift Sets",
    "home-fragrance": "Home Fragrance",
    "beauty-body": "Beauty & Body",
    "new-arrivals": "New Arrivals",
    "scent-families": "Scent Families",
    "fragrance-guides": "Fragrance Guides",
    "editorial-picks": "Editorial Product Picks",
}


def display_board_name(board_key: str) -> str:
    return BOARD_ALIASES.get(board_key, board_key.replace("-", " ").title())

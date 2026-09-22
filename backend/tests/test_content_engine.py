from decimal import Decimal
from app.services.content_engine import ProductFacts, propose_content
from app.services.copy_engine import generate_fact_safe_copy


def product(**kwargs):
    base = dict(
        product_id="p1", title="Example Eau de Parfum", vendor="Example Brand",
        product_type="Fragrance", price=Decimal("79.00"), inventory_total=10,
        product_url="https://diamondshelf.us/products/example",
    )
    base.update(kwargs)
    return ProductFacts(**base)


def test_note_angle_only_when_note_is_known():
    without = {p.angle_key for p in propose_content(product(notes=()))}
    with_note = {p.angle_key for p in propose_content(product(notes=("Vanilla",)))}
    assert "note-vanilla" not in without
    assert "note-vanilla" in with_note


def test_out_of_stock_has_no_proposals():
    assert propose_content(product(inventory_total=0)) == []


def test_arabian_taxonomy_maps_to_canonical_arabian_angle_and_board():
    item = product(is_arabian=True)
    proposals = propose_content(item)
    assert proposals[0].board_key == "arabian-fragrance"

    arabian = [p for p in proposals if p.angle_key == "arabian-fragrance"]
    assert len(arabian) == 1
    proposal = arabian[0]
    assert proposal.angle_label == "Arabian Fragrance Discovery"
    assert proposal.board_key == "arabian-fragrance"
    assert proposal.keywords == (
        "arabian perfume",
        "middle eastern fragrance",
        "arabian fragrance",
    )
    assert proposal.reason == (
        "Product taxonomy explicitly classifies the item as Arabian fragrance."
    )
    assert all(
        p.angle_key != "arabian-fragrance-discovery"
        for p in proposals
    )
    assert generate_fact_safe_copy(item, proposal).title.endswith(
        "| Arabian Fragrance Discovery"
    )


def test_arabian_key_change_preserves_new_arrival_proposal():
    proposals = propose_content(
        product(is_arabian=True, is_new_arrival=True),
        limit=20,
    )
    new_arrival = [p for p in proposals if p.angle_key == "new-arrival"]
    assert len(new_arrival) == 1
    proposal = new_arrival[0]
    assert proposal.angle_label == "New Fragrance Arrival"
    assert proposal.board_key == "new-arrivals"
    assert proposal.keywords == (
        "new perfume",
        "new fragrance",
        "fragrance new arrivals",
    )
    assert proposal.reason == "Product is explicitly marked as a new arrival."


def test_copy_does_not_invent_note_for_generic_spotlight():
    p = product(notes=())
    proposal = propose_content(p)[0]
    copy = generate_fact_safe_copy(p, proposal)
    assert "vanilla" not in copy.description.lower()
    assert "oud" not in copy.description.lower()

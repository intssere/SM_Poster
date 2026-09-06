"""Read-only destination verification and pure immutable-snapshot mapping."""
from collections import Counter
from urllib.parse import parse_qsl, urlsplit

from app.core.config import Settings
from app.models.domain import PinPublication
from app.integrations.buffer.gateway import (
    BufferConfigurationError, BufferGateway, BufferPinterestPostPayload, public_url, required,
)


def build_pinterest_payload(publication: PinPublication, settings: Settings) -> BufferPinterestPostPayload:
    if public_url(publication.utm_url, https_only=False) != public_url(publication.destination_url, https_only=False):
        raise BufferConfigurationError("BUFFER_DESTINATION_MISMATCH")
    destination, tracked = urlsplit(publication.destination_url), urlsplit(publication.utm_url)
    # Tracking may add UTM parameters, not silently change product/variant selection.
    original_query = Counter(parse_qsl(destination.query, keep_blank_values=True))
    tracked_query = Counter((k, v) for k, v in parse_qsl(tracked.query, keep_blank_values=True) if not k.lower().startswith("utm_"))
    original_query = Counter({pair: count for pair, count in original_query.items() if not pair[0].lower().startswith("utm_")})
    if original_query != tracked_query or destination.fragment != tracked.fragment:
        raise BufferConfigurationError("BUFFER_DESTINATION_MISMATCH")
    payload = BufferPinterestPostPayload(
        channel_id=required(settings.buffer_pinterest_channel_id),
        board_service_id=publication.pinterest_board_id_snapshot,
        title=publication.title_snapshot, text=publication.description_snapshot,
        url=publication.utm_url, image_url=publication.media_url_snapshot,
        alt_text=publication.alt_text_snapshot,
    )
    payload.to_input()  # Validate without changing any material snapshot value.
    return payload


async def verify_destination(publication: PinPublication, settings: Settings, gateway: BufferGateway) -> dict:
    organization_id = required(settings.buffer_organization_id)
    channel_id = required(settings.buffer_pinterest_channel_id)
    board_id = required(publication.pinterest_board_id_snapshot)
    if not any(o["id"] == organization_id for o in await gateway.organizations()):
        raise BufferConfigurationError("BUFFER_ORGANIZATION_NOT_FOUND")
    matches = [c for c in await gateway.channels(organization_id) if c["id"] == channel_id]
    if len(matches) != 1:
        raise BufferConfigurationError("BUFFER_CHANNEL_NOT_FOUND")
    channel = matches[0]
    if channel["service"] != "pinterest" or channel["isDisconnected"] or channel["isLocked"]:
        raise BufferConfigurationError("BUFFER_CHANNEL_UNAVAILABLE")
    if not any(b["serviceId"] == board_id for b in channel["boards"]):
        raise BufferConfigurationError("BUFFER_BOARD_NOT_FOUND")
    # Not approval, duplicate clearance, or permission to dispatch.
    return {"organization_id": organization_id, "channel_id": channel_id, "board_service_id": board_id}

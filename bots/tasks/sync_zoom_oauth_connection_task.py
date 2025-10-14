import logging

import requests
from django.db import transaction
from django.utils import timezone

from bots.models import WebhookTriggerTypes, ZoomMeetingToZoomOAuthConnectionMapping, ZoomOAuthConnection, ZoomOAuthConnectionStates
from bots.webhook_payloads import zoom_oauth_connection_webhook_payload
from bots.webhook_utils import trigger_webhook

logger = logging.getLogger(__name__)

from celery import shared_task


class ZoomAPIError(Exception):
    """Custom exception for Zoom API errors."""

    pass


class ZoomAPIAuthenticationError(ZoomAPIError):
    """Custom exception for Zoom API errors."""

    pass


def _raise_if_error_is_authentication_error(e: requests.RequestException):
    error_code = e.response.json().get("error")
    if error_code == "invalid_grant" or error_code == "invalid_client":
        raise ZoomAPIAuthenticationError(f"Zoom Authentication error: {e.response.json()}")

    return


def _get_access_token(zoom_oauth_connection) -> str:
    """
    Exchange the stored refresh token for a new access token.
    Zoom returns a new refresh_token on each successful refresh.
    Persist it so we don't lose the chain.
    """
    credentials = zoom_oauth_connection.get_credentials()
    if not credentials:
        raise ZoomAPIAuthenticationError("No credentials found for zoom oauth connection")

    refresh_token = credentials.get("refresh_token")
    client_id = zoom_oauth_connection.zoom_oauth_app.client_id
    client_secret = zoom_oauth_connection.zoom_oauth_app.client_secret
    if not refresh_token or not client_id or not client_secret:
        raise ZoomAPIAuthenticationError("Missing refresh_token or client_secret")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }

    try:
        response = requests.post("https://zoom.us/oauth/token", data=data, timeout=30)
        response.raise_for_status()
        token_data = response.json()

        access_token = token_data.get("access_token")
        if not access_token:
            raise ZoomAPIError(f"No access_token in refresh response. Response body: {response.json()}")

        # IMPORTANT: Zoom rotates refresh tokens. Save the new one if provided.
        new_refresh = token_data.get("refresh_token")
        if new_refresh and new_refresh != refresh_token:
            credentials["refresh_token"] = new_refresh
            zoom_oauth_connection.set_credentials(credentials)
            logger.info("Stored rotated Zoom refresh_token for zoom oauth connection %s", zoom_oauth_connection.object_id)

        return access_token

    except requests.RequestException as e:
        _raise_if_error_is_authentication_error(e)
        raise ZoomAPIError(f"Failed to refresh Zoom access token. Response body: {e.response.json()}")


def _make_zoom_api_request(url: str, access_token: str, params: dict) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}

    req = requests.Request("GET", url, headers=headers, params=params).prepare()
    try:
        # Send the request
        with requests.Session() as s:
            resp = s.send(req, timeout=25)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        _raise_if_error_is_authentication_error(e)
        logger.exception(f"Failed to make Zoom API request. Response body: {e.response.json()}")
        raise e


def _get_zoom_personal_meeting_id(access_token: str) -> str:
    base_url = "https://api.zoom.us/v2/users/me"
    response_data = _make_zoom_api_request(base_url, access_token, {})
    return response_data.get("pmi")


def _get_zoom_meetings(access_token: str) -> list[dict]:
    base_url = "https://api.zoom.us/v2/users/me/meetings"
    base_params = {
        "page_size": 300,
    }

    all_meetings = []
    next_page_token = None

    while True:
        params = dict(base_params)  # copy base params
        if next_page_token:
            params["next_page_token"] = next_page_token

        logger.info(f"Fetching Zoom meetings: {base_url} with params: {params}")
        response_data = _make_zoom_api_request(base_url, access_token, params)

        meetings = response_data.get("meetings", [])
        all_meetings.extend(meetings)

        next_page_token = response_data.get("next_page_token")
        if not next_page_token:
            break

    return all_meetings


def _upsert_zoom_meeting_to_zoom_oauth_connection_mapping(zoom_meeting_ids: list[int], zoom_oauth_connection: ZoomOAuthConnection):
    zoom_oauth_app = zoom_oauth_connection.zoom_oauth_app
    num_updated = 0
    num_created = 0

    # Iterate over the zoom meetings and upsert the zoom meeting to zoom oauth connection mapping
    for zoom_meeting_id in zoom_meeting_ids:
        if not zoom_meeting_id:
            logger.warning(f"Zoom meeting id is None for zoom oauth connection {zoom_oauth_connection.id}")
            continue

        zoom_meeting_to_zoom_oauth_connection_mapping, created = ZoomMeetingToZoomOAuthConnectionMapping.objects.update_or_create(
            zoom_oauth_app=zoom_oauth_app,
            meeting_id=zoom_meeting_id,
            defaults={"zoom_oauth_connection": zoom_oauth_connection},
        )
        # If one already exists, but it has a different zoom_oauth_connection_id, update it
        if not created and zoom_meeting_to_zoom_oauth_connection_mapping.zoom_oauth_connection_id != zoom_oauth_connection.id:
            zoom_meeting_to_zoom_oauth_connection_mapping.zoom_oauth_connection = zoom_oauth_connection
            zoom_meeting_to_zoom_oauth_connection_mapping.save()
            num_updated += 1
        if created:
            num_created += 1

    logger.info(f"Upserted {num_updated} zoom meeting ids to zoom oauth connection mappings and created {num_created} new ones for zoom oauth connection {zoom_oauth_connection.id}")


def enqueue_sync_zoom_oauth_connection_task(zoom_oauth_connection: ZoomOAuthConnection):
    """Enqueue a sync zoom oauth connection task for a zoom oauth connection."""
    with transaction.atomic():
        zoom_oauth_connection.sync_task_enqueued_at = timezone.now()
        zoom_oauth_connection.sync_task_requested_at = None
        zoom_oauth_connection.save()
        sync_zoom_oauth_connection.delay(zoom_oauth_connection.id)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,  # Enable exponential backoff
    max_retries=6,
)
def sync_zoom_oauth_connection(self, zoom_oauth_connection_id):
    """Celery task to sync zoom meetings with a zoom oauth connection."""
    logger.info(f"Syncing zoom oauth connection {zoom_oauth_connection_id}")
    zoom_oauth_connection = ZoomOAuthConnection.objects.get(id=zoom_oauth_connection_id)

    try:
        # Set the sync start time
        sync_started_at = timezone.now()

        access_token = _get_access_token(zoom_oauth_connection)
        zoom_meetings = _get_zoom_meetings(access_token)

        logger.info(f"Fetched {len(zoom_meetings)} meetings from Zoom for zoom oauth connection {zoom_oauth_connection_id}")

        zoom_personal_meeting_id = _get_zoom_personal_meeting_id(access_token)
        zoom_meeting_ids = [zoom_meeting["id"] for zoom_meeting in zoom_meetings] + [zoom_personal_meeting_id]

        _upsert_zoom_meeting_to_zoom_oauth_connection_mapping(zoom_meeting_ids, zoom_oauth_connection)

        # Update zoom oauth connection sync success timestamp and window
        zoom_oauth_connection.last_attempted_sync_at = timezone.now()
        zoom_oauth_connection.last_successful_sync_at = zoom_oauth_connection.last_attempted_sync_at
        zoom_oauth_connection.last_successful_sync_started_at = sync_started_at
        zoom_oauth_connection.state = ZoomOAuthConnectionStates.CONNECTED
        zoom_oauth_connection.connection_failure_data = None
        zoom_oauth_connection.save()

    except ZoomAPIAuthenticationError as e:
        # Update zoom oauth connection state to indicate failure
        with transaction.atomic():
            zoom_oauth_connection.state = ZoomOAuthConnectionStates.DISCONNECTED
            zoom_oauth_connection.connection_failure_data = {
                "error": str(e),
                "timestamp": timezone.now().isoformat(),
            }
            zoom_oauth_connection.save()

        logger.exception(f"Zoom OAuth connection sync failed with ZoomAPIAuthenticationError for {zoom_oauth_connection_id}: {e}")

        # Create webhook event
        trigger_webhook(
            webhook_trigger_type=WebhookTriggerTypes.ZOOM_OAUTH_CONNECTION_STATE_CHANGE,
            zoom_oauth_connection=zoom_oauth_connection,
            payload=zoom_oauth_connection_webhook_payload(zoom_oauth_connection),
        )

    except Exception as e:
        logger.exception(f"Zoom OAuth connection sync failed with {type(e).__name__} for {zoom_oauth_connection_id}: {e}")
        zoom_oauth_connection.last_attempted_sync_at = timezone.now()
        zoom_oauth_connection.save()
        raise

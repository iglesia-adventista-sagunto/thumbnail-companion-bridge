import logging
import os
import re

logger = logging.getLogger("thumbnail-bridge.youtube")

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube"]

try:
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    YOUTUBE_LIBS_AVAILABLE = True
except ImportError:
    YOUTUBE_LIBS_AVAILABLE = False
    GoogleAuthRequest = None
    Credentials = None
    Flow = None
    build = None
    MediaFileUpload = None


_LICENSE_LINE_RE = re.compile(
    r"^(\s*(?:miniatura(?:\s*/\s*thumbnail)?|thumbnail(?:\s*/\s*miniatura)?|portada)\s*:)\s*(.*)$",
    re.IGNORECASE,
)
_LICENSES_HEADER_RE = re.compile(r"^\s*(?:licencias|licenses|licencia|license)\s*:?\s*$", re.IGNORECASE)


def format_livestream_title(title, subtitle=""):
    """Format YouTube livestream title as '[title] - [subtitle]'."""
    clean_title = str(title or "").strip()
    clean_subtitle = str(subtitle or "").strip()
    if clean_title and clean_subtitle:
        return f"{clean_title} - {clean_subtitle}"
    return clean_title or clean_subtitle


def update_description_thumbnail_license(description, new_license):
    """
    Update only the 'miniatura/thumbnail' license line in the Licenses section
    at the bottom of a YouTube livestream description.
    """
    desc = str(description or "")
    license_text = str(new_license or "").strip()
    if not license_text:
        return desc

    lines = desc.splitlines()

    # Search from the bottom up so we only touch the Licenses list at the bottom
    for idx in range(len(lines) - 1, -1, -1):
        match = _LICENSE_LINE_RE.match(lines[idx])
        if match:
            label = match.group(1)
            lines[idx] = f"{label} {license_text}"
            return "\n".join(lines)

    # No existing miniatura/thumbnail line; look for a Licenses header near the bottom
    for idx in range(len(lines) - 1, -1, -1):
        if _LICENSES_HEADER_RE.match(lines[idx]):
            insert_at = idx + 1
            while insert_at < len(lines) and lines[insert_at].strip():
                insert_at += 1
            lines.insert(insert_at, f"Miniatura: {license_text}")
            return "\n".join(lines)

    # Fallback: append a Licenses block at the bottom
    trimmed = desc.rstrip()
    if not trimmed:
        return f"Licencias:\nMiniatura: {license_text}"
    return f"{trimmed}\n\nLicencias:\nMiniatura: {license_text}"


class YouTubeService:
    """Optional YouTube Data API v3 integration for livestream metadata & thumbnail updates."""

    def __init__(self, client_secrets_file, token_file):
        self.client_secrets_file = client_secrets_file
        self.token_file = token_file
        self._pending_flows = {}

    def _load_credentials(self):
        if not YOUTUBE_LIBS_AVAILABLE:
            return None
        if not self.token_file or not os.path.exists(self.token_file):
            return None
        creds = Credentials.from_authorized_user_file(self.token_file, YOUTUBE_SCOPES)
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(GoogleAuthRequest())
                self._save_credentials(creds)
            except Exception as e:
                logger.warning("Failed to refresh YouTube OAuth token: %s", e)
                return None
        return creds if (creds and creds.valid) else None

    def _save_credentials(self, creds):
        os.makedirs(os.path.dirname(self.token_file) or ".", exist_ok=True)
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    def get_status(self):
        secrets_exists = bool(self.client_secrets_file and os.path.exists(self.client_secrets_file))
        creds = self._load_credentials() if YOUTUBE_LIBS_AVAILABLE else None
        return {
            "libs_available": YOUTUBE_LIBS_AVAILABLE,
            "client_secrets_exists": secrets_exists,
            "authenticated": bool(creds and creds.valid),
            "client_secrets_file": self.client_secrets_file,
            "token_file": self.token_file,
        }

    def start_auth(self, redirect_uri):
        if not YOUTUBE_LIBS_AVAILABLE:
            raise RuntimeError("Google API libraries are not installed (google-api-python-client, google-auth-oauthlib).")
        if not os.path.exists(self.client_secrets_file):
            raise FileNotFoundError(f"YouTube client_secrets.json not found at: {self.client_secrets_file}")

        if str(redirect_uri).startswith(("http://localhost", "http://127.0.0.1")):
            os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

        flow = Flow.from_client_secrets_file(
            self.client_secrets_file,
            scopes=YOUTUBE_SCOPES,
            redirect_uri=redirect_uri,
        )
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        self._pending_flows[state] = flow
        return auth_url, state

    def disconnect(self):
        self._pending_flows.clear()
        if self.token_file and os.path.exists(self.token_file):
            os.remove(self.token_file)
            return True
        return False

    def finish_auth(self, state, code):
        if not YOUTUBE_LIBS_AVAILABLE:
            raise RuntimeError("Google API libraries are not installed.")
        flow = self._pending_flows.pop(state, None)
        if flow is None:
            raise ValueError("Invalid or expired OAuth state. Please restart YouTube authorization.")
        flow.fetch_token(code=code)
        creds = flow.credentials
        self._save_credentials(creds)
        return True

    def _build_client(self):
        if not YOUTUBE_LIBS_AVAILABLE:
            raise RuntimeError("Google API libraries are not installed.")
        creds = self._load_credentials()
        if not creds:
            raise PermissionError("YouTube account is not authenticated. Complete OAuth setup first.")
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    def list_broadcasts(self, statuses=("upcoming", "active")):
        youtube = self._build_client()
        items = []
        seen = set()
        for st in statuses:
            resp = (
                youtube.liveBroadcasts()
                .list(part="snippet,status", broadcastStatus=st, maxResults=10)
                .execute()
            )
            for b in resp.get("items", []):
                bid = b.get("id")
                if not bid or bid in seen:
                    continue
                seen.add(bid)
                snippet = b.get("snippet", {})
                status_info = b.get("status", {})
                items.append(
                    {
                        "id": bid,
                        "title": snippet.get("title", ""),
                        "description": snippet.get("description", ""),
                        "scheduled_start": snippet.get("scheduledStartTime"),
                        "lifecycle_status": status_info.get("lifeCycleStatus", st),
                    }
                )
        return items

    def resolve_broadcast_id(self, broadcast_id=None):
        if broadcast_id:
            return str(broadcast_id).strip()
        broadcasts = self.list_broadcasts(statuses=("active", "upcoming"))
        if not broadcasts:
            raise ValueError("No active or upcoming YouTube livestreams found. Provide a broadcast_id explicitly.")
        return broadcasts[0]["id"]

    def get_video_snippet(self, video_id):
        youtube = self._build_client()
        resp = youtube.videos().list(part="snippet", id=video_id).execute()
        items = resp.get("items", [])
        if not items:
            raise ValueError(f"YouTube broadcast/video '{video_id}' not found.")
        return items[0]["snippet"]

    def update_livestream(self, broadcast_id=None, title=None, new_license=None, description_override=None, thumbnail_path=None):
        youtube = self._build_client()
        target_id = self.resolve_broadcast_id(broadcast_id)
        snippet = self.get_video_snippet(target_id)

        final_title = str(title).strip() if title else snippet.get("title", "")
        base_desc = description_override if description_override is not None else snippet.get("description", "")
        final_desc = update_description_thumbnail_license(base_desc, new_license) if new_license else base_desc

        # YouTube video title max length is 100 characters
        if len(final_title) > 100:
            final_title = final_title[:97].rstrip() + "..."

        snippet["title"] = final_title
        snippet["description"] = final_desc

        youtube.videos().update(
            part="snippet",
            body={
                "id": target_id,
                "snippet": snippet,
            },
        ).execute()

        thumbnail_updated = False
        if thumbnail_path and os.path.isfile(thumbnail_path):
            media = MediaFileUpload(thumbnail_path, mimetype="image/png", resumable=False)
            youtube.thumbnails().set(videoId=target_id, media_body=media).execute()
            thumbnail_updated = True

        return {
            "status": "ok",
            "broadcast_id": target_id,
            "title": final_title,
            "description": final_desc,
            "thumbnail_updated": thumbnail_updated,
        }

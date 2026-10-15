"""Apply the live tracking mode without committing the map editor's draft."""
from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .storage import (BPS_FILE_LOCK, assert_layout_writable, finish_persistence,
                      get_bps_data_for_edit, layout_needs_reload, layout_revision,
                      save_bps_data)


class BPSTrackingSettingsAPI(HomeAssistantView):
    url = "/api/bps/tracking_settings"
    name = "api:bps:tracking_settings"
    requires_auth = True

    async def post(self, request):
        hass = request.app["hass"]
        try:
            body = await request.json()
        except (ValueError, TypeError, UnicodeError):
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        if (not isinstance(body, dict) or set(body) != {"long_range_intervals", "layout_revision"}
                or type(body.get("long_range_intervals")) is not bool):
            return web.json_response({"error": "Provide a boolean long_range_intervals and the saved layout revision"}, status=400)
        try:
            async with BPS_FILE_LOCK:
                assert_layout_writable(hass)
                layout = get_bps_data_for_edit(hass)
                if not isinstance(layout, dict) or not layout.get("floor"):
                    return web.json_response({"error": "Save a floor plan before changing tracking mode"}, status=400)
                if body["layout_revision"] != layout_revision(layout):
                    return web.json_response({"error": "Saved layout changed. Reload BPS before changing tracking mode; preserve any map drafts first."}, status=409)
                settings = layout.get("outdoor_tracking", {})
                settings = dict(settings) if isinstance(settings, dict) else {}
                settings["long_range_intervals"] = body["long_range_intervals"]
                if body["long_range_intervals"]:
                    settings["enabled"] = True
                layout["outdoor_tracking"] = settings

                async def commit():
                    await save_bps_data(hass, layout)
                    return web.json_response({"outdoor_tracking": settings,
                                              "layout_revision": layout_revision(layout)})
                return await finish_persistence(commit())
        except (OSError, ValueError, TypeError):
            status = 503 if layout_needs_reload(hass) else 500
            return web.json_response({"error": "Tracking mode could not be verified. Reload BPS before changing settings; preserve any map drafts first."}, status=status)

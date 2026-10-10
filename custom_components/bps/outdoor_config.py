"""Validate only the optional extension at the existing atomic save boundary.

Runtime readers normalize defensively and leave old layouts untouched. Editor
saves reject malformed new fields before any map upload or Store write, rather
than replacing a user's complete layout with silently discarded polygons.
"""
import re

from .environment import (BUILDING_BOUNDARY_WEIGHTS, ENVIRONMENT_TYPES,
                          MAX_ENVIRONMENT_POLYGONS, MAX_ENVIRONMENT_VERTICES,
                          RECEIVER_POLICIES, finite_number, normalize_environment)
from .tracker_groups import MAX_GROUPS, MAX_BEACONS_PER_GROUP

SLUG = re.compile(r"[a-z0-9_]{1,64}\Z", re.ASCII)


def validate_outdoor_layout(layout, known_trackers=None):
    """Validate shape; when inventory is supplied, reject synthetic group members.

    Loading persisted data has no Bermuda inventory yet. Save callers supply
    actual tracker keys; runtime normalization independently checks provenance.
    """
    if not isinstance(layout, dict):
        return None
    if "outdoor_tracking" in layout:
        settings = layout["outdoor_tracking"]
        if not isinstance(settings, dict):
            return "outdoor_tracking must be an object"
        for key in ("enabled", "show_uncertainty"):
            if key in settings and not isinstance(settings[key], bool):
                return f"outdoor_tracking.{key} must be true or false"
        if "hide_uncertainty_below_m" in settings and finite_number(
                settings["hide_uncertainty_below_m"], minimum=0, maximum=10000) is None:
            return "Uncertainty hide threshold must be a finite number from 0 to 10000 metres"
    floors = layout.get("floor", [])
    for floor in floors if isinstance(floors, list) else []:
        if not isinstance(floor, dict):
            continue
        if "environment" in floor:
            regions = floor["environment"]
            if not isinstance(regions, list) or len(regions) > MAX_ENVIRONMENT_POLYGONS:
                return f"Environment must be a list of at most {MAX_ENVIRONMENT_POLYGONS} polygons"
            identifiers = set()
            for region in regions:
                if not isinstance(region, dict):
                    return "Each environment polygon must be an object"
                identifier = region.get("id")
                if not isinstance(identifier, str) or not identifier or len(identifier) > 128 or identifier in identifiers:
                    return "Environment polygon IDs must be nonempty and unique on their floor"
                identifiers.add(identifier)
                name = region.get("name")
                if not isinstance(name, str) or not name.strip() or len(name) > 256:
                    return "Environment polygon names must be nonempty text of at most 256 characters"
                kind, material = region.get("type"), region.get("material", "unknown")
                if not isinstance(kind, str) or kind not in ENVIRONMENT_TYPES:
                    return "Unknown environment polygon type"
                if not isinstance(material, str) or material not in BUILDING_BOUNDARY_WEIGHTS:
                    return "Unknown building material"
                points = region.get("points")
                if not isinstance(points, list) or not 3 <= len(points) <= MAX_ENVIRONMENT_VERTICES:
                    return f"Environment polygons need 3–{MAX_ENVIRONMENT_VERTICES} vertices"
            if len(normalize_environment(regions)) != len(regions):
                return "Environment polygons must be finite, nonempty and free of self-intersections"
        receivers = floor.get("receivers", [])
        for receiver in receivers if isinstance(receivers, list) else []:
            if isinstance(receiver, dict) and "outdoor_policy" in receiver:
                policy = receiver["outdoor_policy"]
                if not isinstance(policy, str) or policy not in RECEIVER_POLICIES:
                    return "Unknown outdoor receiver policy"
    if "tracker_groups" in layout:
        groups = layout["tracker_groups"]
        if not isinstance(groups, list) or len(groups) > MAX_GROUPS:
            return f"tracker_groups must be a list of at most {MAX_GROUPS} groups"
        outputs = {f"bps_group_{g['id']}" for g in groups
                   if isinstance(g, dict) and isinstance(g.get("id"), str) and SLUG.fullmatch(g["id"])}
        identifiers = set()
        for group in groups:
            if not isinstance(group, dict):
                return "Each tracker group must be an object"
            identifier = group.get("id")
            if not isinstance(identifier, str) or not SLUG.fullmatch(identifier) or identifier in identifiers:
                return "Group IDs must be unique lowercase slugs (letters, digits and underscores)"
            identifiers.add(identifier)
            if "enabled" in group and not isinstance(group["enabled"], bool):
                return "Group enabled must be true or false"
            if not isinstance(group.get("name", identifier), str):
                return "Group names must be text"
            beacons = group.get("beacons")
            if not isinstance(beacons, list) or not 1 <= len(beacons) <= MAX_BEACONS_PER_GROUP:
                return f"Groups need 1–{MAX_BEACONS_PER_GROUP} beacon slugs"
            if any(not isinstance(b, str) or not SLUG.fullmatch(b) or (
                    known_trackers is not None and b in outputs and b not in known_trackers) for b in beacons):
                return "Groups must reference individual BPS tracker slugs"
    return None

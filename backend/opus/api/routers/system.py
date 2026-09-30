"""The runtime settings the Settings page edits. They describe the installation
itself, not a catalog resource."""

from fastapi import APIRouter, HTTPException

from opus import provision
from opus.settings_store import SettingsValidationError, get_for_ui, update_settings

router = APIRouter()


@router.get("/settings")
async def read_settings():
    return await get_for_ui()


@router.put("/settings")
async def write_settings(updates: dict[str, str]):
    try:
        previous = await update_settings(updates)
    except SettingsValidationError as exc:
        raise HTTPException(400, {"key": exc.key, "code": exc.code})
    await provision.retire(previous)
    provision.apply_saved(previous)
    return await get_for_ui()

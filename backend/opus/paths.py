"""Which directories a setting may name. A landing directory is written into and
emptied by OPUS itself, and a share directory is bound into an engine that
offers it to a peer network, so both are held to the trees this install was
given for that purpose — never the filesystem at large."""

from pathlib import PurePosixPath

from opus.config import settings

_NEVER = {PurePosixPath(p) for p in (
    "/", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/usr", "/var", "/home",
    "/opt", "/srv", "/mnt", "/media", "/tmp",
)}
_NEVER_INSIDE = tuple(PurePosixPath(p) for p in (
    "/proc", "/sys", "/dev", "/etc", "/boot", "/root", "/run",
))


def _absolute(value: str) -> PurePosixPath | None:
    if not value.startswith("/"):
        return None
    path = PurePosixPath(value)
    return None if ".." in path.parts else path


def _system(path: PurePosixPath) -> bool:
    return path in _NEVER or any(path == p or path.is_relative_to(p) for p in _NEVER_INSIDE)


def _inside(value: str, root: str) -> bool:
    path, base = _absolute(value), _absolute(root)
    if path is None or base is None or _system(path) or _system(base):
        return False
    return path.is_relative_to(base)


def landing_refusal(value: str) -> str | None:
    """Why a landing directory is refused, as the code the settings form shows,
    or None when it is inside the landing zone."""
    if _absolute(value) is None:
        return "bad_path"
    return None if _inside(value, settings.landing_root) else "outside_landing"


def share_refusal(value: str) -> str | None:
    if _absolute(value) is None:
        return "bad_path"
    if not _inside(value, settings.media_host_dir):
        return "outside_media"
    engines = PurePosixPath(settings.engines_host_dir)
    if engines.is_relative_to(PurePosixPath(value)):
        return "outside_media"
    return None

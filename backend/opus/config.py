from opus_auth import SESSION_KEY_MIN
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Infra bootstrap, read from env at startup. User-editable settings
    (engine modes, URLs, credentials) live in the DB — see
    opus.settings_store."""

    model_config = {"env_prefix": "OPUS_"}

    database_url: str = Field(min_length=1)
    cookie_domain: str = ""
    # where Library answers, which keeps the roster this module asks, and the
    # service token it is asked with. There is no install without them: an
    # unset address would be a door with no lock.
    auth_url: str = Field(min_length=1)
    auth_token: str = Field(min_length=1)
    # what sessions are signed with, the same in all three modules
    session_key: str = Field(min_length=SESSION_KEY_MIN)

    # the origin opus.engine_ui answers on, which a browser frames the bundled
    # engines' own interfaces from
    engines_url: str = Field(default="", pattern=r"^(https?://[^/\s]+)?$")

    search_timeout_seconds: int = 45

    # The landing tree root every bundled engine downloads into. Mounted into
    # the container.
    landing_root: str = "/landing"

    # Identity and clock every bundled engine runs with — one install, one set
    # of service settings rather than eight subtly different ones.
    engine_uid: int = 1000
    engine_gid: int = 1000
    timezone: str = "Europe/Zagreb"
    # Docker's API takes bytes rather than Compose's friendly `2g` spelling.
    # Bundled engines and their VPN tunnels are created by the controller, so
    # their limits live with the controller's immutable contract.
    engine_memory_limit_bytes: int = Field(default=2 * 1024**3, gt=0)
    engine_pids_limit: int = Field(default=256, gt=0)
    vpn_memory_limit_bytes: int = Field(default=512 * 1024**2, gt=0)
    vpn_pids_limit: int = Field(default=128, gt=0)

    # The network OPUS joins the engines it starts to, so it can reach them by
    # container name. The database is not on it, and nothing an engine serves
    # is published to the host.
    engine_network: str = "opus-downloads_engines"

    # The socket belongs only to the internal Docker controller.  The web
    # backend talks to that controller, whose fixed operation contract prevents
    # a request from becoming an arbitrary host container.
    docker_socket: str = "/var/run/docker.sock"
    docker_controller_url: str = "http://docker-controller:2376"

    # Where the bundled engines' configuration lives — one directory per engine.
    # Mounted into the container.
    engines_dir: str = "/engines"

    # The same two trees as the DOCKER HOST sees them. A bind is resolved by the
    # daemon, not inside this container, so handing it the paths above would
    # silently create empty directories next to the real ones.
    engines_host_dir: str = "/mnt/docker/opus-downloads/volumes/engines"
    landing_host_dir: str = "/mnt/docker/opus-downloads/volumes/landing"
    # the media tree, as the Docker host sees it, that an engine may offer back
    # to its network (slskd's share). Unset, nothing is shared.
    media_host_dir: str = ""


settings = Settings()

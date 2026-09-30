# plugins

Code an installation adds that this repository does not carry, one directory
per plugin, each with an `opus-plugin.toml` naming what it hands each module
(`backend/opus_core/plugins.py`). The backend image installs a plugin's
`requirements/opus-downloads.lock` and mounts this directory at `/plugins`.

`.env` may name another directory as `OPUS_PLUGINS`, where the plugins are
checked out as git repositories: `./check.sh` runs their tests with the
module's and vouches for their commits, and `deploy/prod.sh` ships each one
here, under `plugins/<name>`, on the target.

An installation without plugins leaves this directory as it is.

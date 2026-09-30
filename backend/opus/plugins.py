"""What the installation's plugins add to Downloads (opus_core.plugins).

A plugin hands this module a `Plugin`: engines the repository does not carry,
each as the spec the catalog lists and the dotted path of its adapter class,
and the words its engines are shown with. The class is imported only when an
engine is built, so the plugin may import anything of Downloads while the
catalog, which everything imports, is still being put together."""

from dataclasses import dataclass, field

from opus_core import plugins

from opus.engines.spec import EngineSpec


@dataclass(frozen=True)
class Plugin:
    # (spec, "package.module:Class")
    engines: tuple[tuple[EngineSpec, str], ...] = ()
    words: dict[str, dict[str, str]] = field(default_factory=dict)


PLUGINS: tuple[Plugin, ...] = plugins.load("opus-downloads")
if not all(isinstance(plugin, Plugin) for plugin in PLUGINS):
    raise plugins.PluginError("a plugin for opus-downloads must hand over an opus.plugins.Plugin")

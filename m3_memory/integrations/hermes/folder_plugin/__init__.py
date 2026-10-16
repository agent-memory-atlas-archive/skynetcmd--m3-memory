"""m3 memory provider for Hermes Agent: a delegating stub.

`m3 setup` copies this file (with plugin.yaml) into Hermes' plugins/memory/m3/
for Hermes versions that predate the hermes_agent.memory_providers entry point.
The provider itself lives in the installed m3-memory package, so a copied
folder can never go stale: upgrading m3-memory upgrades the provider. Requires
m3-memory installed in Hermes' own Python environment.
"""

from m3_memory.integrations.hermes import M3MemoryProvider, register  # noqa: F401  (Hermes loads MemoryProvider via register)

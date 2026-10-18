"""The last section of `m3 doctor`: m3 settings still set as M3_* variables.

Each is deprecated in favour of m3_config.toml, and the section names the
`m3 config set` that replaces it. The sweep and the wording are owned by
``m3_memory.install.sections.m3_env_settings_section``; this probe only runs it
last, after every other check. Exit-code neutral: a deprecated variable still
works, so it is not a failure.
"""

from __future__ import annotations


def run(brief: bool = True) -> int:
    try:
        from m3_memory.install.sections import config_root_section, llm_enrichment_section, m3_env_settings_section
    except ImportError as e:
        print(f"M3_* environment variables: not checked (m3_memory not importable: {e})")
        return 0
    llm_enrichment_section(brief=brief)
    config_root_section(brief=brief)
    m3_env_settings_section(brief=brief)
    return 0

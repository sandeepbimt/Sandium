# Sandium BuildBuddy REAPI backend configuration.
#
# Starting point only: the corresponding reference proved this OS constraint
# for a small Chromium M151 compile, not for Sandium's pinned Chromium 156.
# Keep platform properties minimal until a real Sandium action proves more
# constraints are required. This file contains no credentials.

load("@builtin//struct.star", "module")

def __platform_properties(ctx):
    return {
        "default": {
            "OSFamily": "Linux",
        },
        "large": {
            "OSFamily": "Linux",
        },
    }

backend = module(
    "backend",
    platform_properties = __platform_properties,
)

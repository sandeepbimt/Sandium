# -*- bazel-starlark -*-
# Copyright 2026 The SANDIUM Authors.
#
# SANDIUM BuildBuddy backend aligned with the successful SANDFOX setup.
# Do not override the worker container image until an image is verified against
# the configured BuildBuddy service. Let the service choose its default Linux
# worker image, matching SANDFOX's minimal backend properties.

load("@builtin//struct.star", "module")

def __platform_properties(ctx):
    return {
        "default": {"OSFamily": "Linux"},
        "large": {"OSFamily": "Linux"},
    }

backend = module(
    "backend",
    platform_properties = __platform_properties,
)

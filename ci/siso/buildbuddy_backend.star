# -*- bazel-starlark -*-
# Copyright 2026 The SANDIUM Authors.
#
# Backend starting point based on Chromium 156's official non-Google RBE
# example. The image is digest-pinned. BuildBuddy pullability and action
# compatibility have not yet been tested.
#
# Do not copy Google's label:action_default / label:action_large labels here.

load("@builtin//struct.star", "module")

_CONTAINER_IMAGE = "docker://gcr.io/chops-public-images-prod/rbe/siso-chromium/linux@sha256:d7cb1ab14a0f20aa669c23f22c15a9dead761dcac19f43985bf9dd5f41fbef3a"

def __platform_properties(ctx):
    return {
        "default": {
            "OSFamily": "Linux",
            "container-image": _CONTAINER_IMAGE,
        },
        "large": {
            "OSFamily": "Linux",
            "container-image": _CONTAINER_IMAGE,
        },
    }

backend = module(
    "backend",
    platform_properties = __platform_properties,
)

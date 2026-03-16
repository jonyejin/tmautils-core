# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

import warnings

warnings.warn(
    "tmautils.common is deprecated. Use tmautils.core instead.",
    DeprecationWarning,
    stacklevel=2,
)

from tmautils.core import *  # noqa: F401, F403

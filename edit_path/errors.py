# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only


class EditPathError(RuntimeError):
    """Base error for a failed reconstruction or validation gate."""


class GateError(EditPathError):
    """A named acceptance gate failed."""

    def __init__(self, gate: str, message: str, sequence: int | None = None):
        super().__init__(message)
        self.gate = gate
        self.sequence = sequence

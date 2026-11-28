# -*- coding: utf-8 -*-
# Fork of BittyTax (AGPL-3.0). See LICENSE.

from __future__ import annotations


class DeskError(ValueError):
    """A problem the desk can show to the person using it."""

    def __init__(self, message: str, problem_id: str = "") -> None:
        super().__init__(message)
        self.problem_id = problem_id

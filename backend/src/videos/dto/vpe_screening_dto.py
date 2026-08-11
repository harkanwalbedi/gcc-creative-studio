# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Wire shape for a cheap VPE eligibility check.

The package's own ``VpeScreeningResult`` is a frozen dataclass whose most
useful member - ``offer`` - is a property, and properties do not survive
serialisation. Restating the answer here keeps the frontend contract
deliberate rather than a side effect of how the library happens to be built.
"""

from pydantic import BaseModel, Field

from src.videos.vpe.gating import VpeScreeningResult


class VpeFindingResponse(BaseModel):
    """One reason a capability was or was not ruled out."""

    code: str
    severity: str
    message: str
    remedy: str = ""
    measured: str = ""
    required: str = ""


class VpeScreeningResponse(BaseModel):
    """Whether a capability is worth offering on a gallery row."""

    capability_id: str
    screening: str = Field(
        description="ruled_out, not_ruled_out, or unknown.",
    )
    offer: bool = Field(
        description=(
            "Whether to show the action. True for both not_ruled_out and"
            " unknown: a row this cannot judge is one where refusing to"
            " offer would hide a capability that may well work."
        ),
    )
    findings: list[VpeFindingResponse] = []

    @classmethod
    def from_result(
        cls,
        result: VpeScreeningResult,
    ) -> "VpeScreeningResponse":
        """Builds the response from a library screening result.

        Args:
            result: What ``screen_stored_video`` returned.

        Returns:
            The same answer, in the shape the frontend receives.
        """
        return cls(
            capability_id=result.capability_id,
            screening=result.screening.value,
            offer=result.offer,
            findings=[
                VpeFindingResponse(
                    code=finding.code.value,
                    severity=finding.severity.value,
                    message=finding.message,
                    remedy=finding.remedy,
                    measured=finding.measured,
                    required=finding.required,
                )
                for finding in result.findings
            ],
        )

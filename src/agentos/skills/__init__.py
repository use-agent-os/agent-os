"""Skills system for AgentOS.

Six-layer architecture (low→high precedence):
- Extra: config-specified additional directories
- Bundled: Ship with AgentOS in src/agentos/skills/bundled/
- Managed: Local installs in $AGENTOS_STATE_DIR/skills/ (default ~/.agentos/skills/)
- Personal: Local user installs in ~/.agents/skills/
- Project: {workspace}/.agents/skills/
- Workspace: {workspace}/skills/

Only Bundled skills are shipped with AgentOS. Managed, Personal, Project,
Workspace, and Extra layers are local directories discovered at runtime.
"""

from __future__ import annotations

from agentos.skills.eligibility import (
    EligibilityContext,
    EligibilityReport,
    InstallHint,
    check_eligibility,
    diagnose_eligibility,
)
from agentos.skills.injector import SkillInjector
from agentos.skills.loader import SkillLoader
from agentos.skills.resources import SkillResources
from agentos.skills.types import (
    SkillInstallSpec,
    SkillLayer,
    SkillPlatformMeta,
    SkillRequires,
    SkillSpec,
)

__all__ = [
    "EligibilityContext",
    "EligibilityReport",
    "InstallHint",
    "SkillInjector",
    "SkillInstallSpec",
    "SkillLayer",
    "SkillLoader",
    "SkillPlatformMeta",
    "SkillRequires",
    "SkillResources",
    "SkillSpec",
    "check_eligibility",
    "diagnose_eligibility",
]

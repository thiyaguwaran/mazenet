# -*- coding: utf-8 -*-
{
    'name': 'Mazenet Access Rights',
    'version': '19.0.1.0.0',
    'category': 'Administration',
    'summary': 'Security group hierarchy for DMT, Technology, software, MIS and the Corporate BU',
    'description': """
Mazenet Access Rights
======================
Defines the standard Mazenet BU hierarchy as security groups for the 4
"standard" teams:

* DMT
* Technology
* software
* MIS

Each team has its own 6-group chain, under its own module category and
privilege, with no group of one team connected (via ``implied_ids`` or
otherwise) to a group of another team:

    Agent -> ATL -> Team Lead -> BU Manager
             ^                   ^
     TL-Direct Agent      Manager-Direct Agent

Agent -> ATL -> Team Lead -> BU Manager is the main write-access chain
(each implying full access of the one before it). TL-Direct Agent and
Manager-Direct Agent are the org chart's "TL-direct" / "Manager-direct"
boxes - agents who report straight to the Team Lead or BU Manager,
skipping a level. They carry the exact same permissions as the plain
Agent group (they simply imply it); they exist as separate groups only so
that reporting line is visible/assignable, not to grant anything extra.

CTO/Admin and MD are the two exceptions: matching the reference org chart's
"All access" / "Read-only, all teams" labels, they are single, global groups
shared by every team rather than duplicated per team.
    * ``group_mzr_cto_admin`` implies every standard team's BU Manager group
      PLUS the shared Corporate BU Manager below, so it has full write
      access everywhere.
    * ``group_mzr_md`` is deliberately *not* part of any team's write chain
      (mirrors the dashed "read-only, not a reporting line" relationship to
      CTO/admin) - wire it into read-only ``ir.rule`` domains, without a
      team filter, when record-level rules are added later.

Corporate BU - a second, structurally different block
-------------------------------------------------------
Five more teams share a SINGLE BU Manager instead of each having their own
(matching the reference org chart's "Corporate BU manager - Shared across 5
groups"):

* Hunter
* Account Manager
* Corporate Training (shown coral in the org chart - new team, not yet in
  the signed annexure)
* LMS
* TNH

Each of the 5 teams still has its own isolated Team Lead -> ATL -> Agent
chain (plus a TL-Direct Agent), exactly like the standard teams - but there
is no per-team BU Manager. Instead, ``group_mzr_corporate_manager`` (under
its own "Corporate" category) implies all 5 teams' Team Lead groups plus
its own Manager-Direct Agent group. That single group is, in turn, implied
by the global CTO/Admin group above. So: the 5 Corporate teams are isolated
from EACH OTHER exactly like the standard teams are, but they intentionally
share one manager - the org chart's explicit exception to the "isolated per
team" rule.

This module only defines the group hierarchy - it does not wire up any
record rules or menus. Combine it with model-specific ``ir.rule`` records
(scoped per team group) where record-level enforcement is needed.
""",
    'author': 'Mazenet Tech / Development Team',
    'website': 'https://www.mazenet.com',
    'depends': ['base'],
    'data': [
        'security/mazenet_access_rights_groups.xml',
    ],
    'demo': [],
    'license': 'AGPL-3',
    'installable': True,
    'application': False,
    'auto_install': False,
}

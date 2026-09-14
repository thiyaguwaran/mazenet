# -*- coding: utf-8 -*-
from odoo import models, fields, api


class CrmStage(models.Model):
    _inherit = "crm.stage"

    # Stock crm.stage has no active field at all - M2-01 asks to "ARCHIVE (do not
    # delete) every stage that is not in these five pipeline sheets", which isn't
    # possible without this. Once present, Odoo's ORM automatically excludes
    # active=False records from every default search - including stage_id's own
    # Many2one domain on the lead form - so no other code needs to change.
    active = fields.Boolean(default=True)

    @api.model
    def _mz_ensure_unassigned_stages(self):
        """Idempotent - creates one "Unassigned" stage per real active crm.team
        EXCEPT DMT, at sequence 0 (every existing stage across every team
        currently starts at sequence 1, per data/stages.xml) so it always sorts
        as the genuine FIRST column (client instruction, 2026-09-15, "important":
        "unassigned stage for all teams as first stage" - a lead transferred from
        DMT/CTO/MD/another team, or one freshly created directly on a team,
        should land here until someone actually picks it up, instead of jumping
        straight to that team's real "New Lead"/working stage).

        DMT itself is excluded (client correction, same day: "for dmt/cto and md
        are excluded for this they no need unassigned stage") - DMT's own 4-stage
        funnel (New Lead/Source -> Lead Validation -> Transfer to BU -> Follow-
        up's) already starts somewhere meaningful, and DMT is where leads
        originate FROM, never a receiving team an Unassigned buffer would matter
        for. CTO/Admin and MD aren't crm.team records at all - they have no
        stages of their own to add one to in the first place, so nothing to
        exclude there beyond noting it.

        Run from a normal (non-noupdate) data file via <function>, not hardcoded
        per-team XML records in the noupdate stages.xml - this way it stays
        correct against WHATEVER teams actually exist right now (several were
        archived as duplicates earlier in this project), not a possibly-stale
        team list baked into XML at build time.

        No code elsewhere needed to actually ROUTE leads here - _mz_team_entry_stage
        (crm_lead.py, used by the Cross-Team Handoff Stage Advance) and stock CRM's
        own _stage_find (used for a brand-new lead's initial stage) already pick
        each team's LOWEST-sequence stage; adding one below everything else is
        enough to become the new entry point for both paths automatically."""
        Stage = self.env['crm.stage']
        dmt_team = self.env.ref('mazenet_crm.team_dmt', raise_if_not_found=False)
        for team in self.env['crm.team'].search([]):
            if team == dmt_team:
                continue
            if Stage.search_count([('name', '=', 'Unassigned'), ('team_ids', 'in', team.id)]):
                continue
            Stage.create({
                'name': 'Unassigned',
                'sequence': 0,
                'fold': False,
                'team_ids': [(4, team.id)],
            })

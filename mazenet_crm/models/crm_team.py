# -*- coding: utf-8 -*-
from odoo import models, fields

class CrmTeam(models.Model):
    _inherit = "crm.team"

    def action_your_pipeline(self):
        """"My Pipeline" (the menu every user actually clicks day-to-day) redirects
        DMT to their own dedicated Pipeline instead (client instruction, 2026-09-15:
        "for dmt redirect their own pipeline menu... hide standard menu pipeline for
        dmt") - ir.ui.menu's own groups_id field can only ever ADD visibility (an
        OR-list of groups), never exclude one team while keeping it for everyone
        else, without an unwieldy explicit allowlist of every OTHER team's groups
        that would silently break the next time a team gets added. Redirecting the
        ACTION itself sidesteps that entirely: DMT keeps clicking the exact same
        "My Pipeline" menu, it just opens crm_lead_action_pipeline_dmt (grouped by
        x_dmt_pipeline_stage_id, drag-enabled - see crm_lead.py) instead of the
        generic crm_lead_action_pipeline every other team gets.

        CTO/Admin and MD (client instruction, 2026-09-21: "no need create lead
        access... hide the New button for them") get the same generic action as
        everyone else, but with its kanban entry swapped for
        mz_crm_lead_kanban_no_create_cto_md (views/crm_lead_views.xml, create="false").
        This has to happen here rather than via ir.rule/ACL: CTO/Admin qualifies for
        perm_create=True through dozens of OTHER teams' own rules via implied_ids,
        and both groups hold base.group_user, whose own crm.lead ACL row already
        grants create=1 model-wide - neither can be "subtracted" from for one
        subgroup, and the list/kanban "create" arch attribute is a static per-view
        boolean with no per-user expression, so swapping in an explicit view_id is
        the only way to get a different New-button behavior out of the exact same
        menu/action everyone else uses. The actual access block (not just the
        button) is the create() guard in crm_lead.py, unconditional and
        independent of which view happened to render the button."""
        if self.env['crm.lead']._mz_user_is_dmt(self.env.user):
            return self.env["ir.actions.actions"]._for_xml_id(
                "mazenet_crm.crm_lead_action_pipeline_dmt"
            )
        action = super().action_your_pipeline()
        user = self.env.user
        if (
            user.has_group('mazenet_access_rights.group_mzr_cto_admin')
            or user.has_group('mazenet_access_rights.group_mzr_md')
        ):
            no_create_view = self.env.ref(
                'mazenet_crm.mz_crm_lead_kanban_no_create_cto_md', raise_if_not_found=False
            )
            if no_create_view and action.get('views'):
                action['views'] = [
                    (no_create_view.id, vtype) if vtype == 'kanban' else (vid, vtype)
                    for vid, vtype in action['views']
                ]
        return action

    # Additional metadata or helpers if needed for Mazenet CRM
    x_bu_category = fields.Selection([
        ('corp', 'Corporate'),
        ('tally', 'Tally'),
        ('dmt', 'DMT'),
        ('tech', 'Technology'),
        ('swdev', 'Software Dev'),
        ('mis', 'MIS'),
        # Corporate Pipeline build (Mazenet_CRM_Corporate_LMS_TNH_Pipelines.xlsx):
        # team_corporate's own 'corp' category above stays for LMS/TNH/Training,
        # still parked there until their own build turns - see teams.xml.
        ('corp_hunter', 'Corporate - Hunter'),
        ('corp_am', 'Corporate - Account Manager'),
        ('corp_training', 'Corporate - Training Delivery'),
        ('lms', 'LMS'),
        ('tnh', 'TNH'),
    ], string="BU Category", default='corp')

    create_lead_id = fields.Many2many('res.users',
        'mazenet_crm_team_create_lead_users_rel', 'team_id', 'user_id',
        string='Create To', domain="[('id', 'in', member_ids)]")

    privelege_ids = fields.Many2many(
        'res.groups.privilege', 'mazenet_crm_team_privilege_rel', 
        'team_id', 'privilege_id',
        string='Privileges')




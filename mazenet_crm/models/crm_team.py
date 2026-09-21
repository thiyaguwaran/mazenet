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

        CTO/Admin and MD get no special handling here at all - super()'s own
        _for_xml_id('crm.crm_lead_action_pipeline') call already goes through
        ir_actions_act_window.py's _get_action_dict() override, which swaps in
        their create="false" views for ANY crm.lead action, not just this one
        (also covers the Leads and Opportunities list views) - see that file for
        why this can't be done via ir.rule/ACL instead."""
        if self.env['crm.lead']._mz_user_is_dmt(self.env.user):
            return self.env["ir.actions.actions"]._for_xml_id(
                "mazenet_crm.crm_lead_action_pipeline_dmt"
            )
        return super().action_your_pipeline()

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




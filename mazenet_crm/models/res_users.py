# -*- coding: utf-8 -*-
from odoo import models, fields, api

class ResUsers(models.Model):
    _inherit = "res.users"

    mz_branch_user_ids = fields.Many2many(
        "res.users",
        "mz_user_branch_rel",
        "user_id",
        "branch_user_id",
        string="Supervision Branch Users",
        compute="_compute_mz_branch_user_ids",
        store=True,
        help="Precomputed transitive subordinate users under this user."
    )

    crm_team_ids = fields.Many2many(
        "crm.team",
        string="User CRM Teams",
        compute="_compute_crm_team_ids",
        help="All CRM teams where the user is a team leader, manager, or member."
    )

    @api.depends()
    def _compute_mz_branch_user_ids(self):
        supervision_obj = self.env['mz.supervision'].sudo()
        for user in self:
            branch_ids = supervision_obj.get_branch(user)
            user.mz_branch_user_ids = [(6, 0, branch_ids)]

    def _compute_crm_team_ids(self):
        team_obj = self.env['crm.team'].sudo()
        for user in self:
            # Teams where user is user_id (leader/manager) or member
            teams = team_obj.search([
                '|',
                ('user_id', '=', user.id),
                ('member_ids', 'in', [user.id])
            ])
            user.crm_team_ids = [(6, 0, teams.ids)]

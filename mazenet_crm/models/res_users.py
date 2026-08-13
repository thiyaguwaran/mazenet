# -*- coding: utf-8 -*-
from odoo import models, fields, api

class ResUsers(models.Model):
    _inherit = "res.users"

    crm_team_ids = fields.Many2many(
        "crm.team",
        string="User CRM Teams",
        compute="_compute_crm_team_ids",
        help="All CRM teams where the user is a team leader, manager, or member."
    )

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

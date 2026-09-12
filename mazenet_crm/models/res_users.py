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

    x_mz_team_id = fields.Many2one(
        "crm.team", string="Sales Team", compute="_compute_x_mz_team_id",
        help="The single crm.team this user is a direct member of - mirrors "
             "crm.lead's own _mz_user_own_team (member_ids-based, one team per "
             "user). Exists purely so the Pipeline search panel's Salesperson "
             "section can group by it (search_panel_select_multi_range needs a "
             "real field on res.users itself - crm_team_ids above is a "
             "Many2many and can hold more than one team, which the search "
             "panel's groupby can't use)."
    )

    def _compute_x_mz_team_id(self):
        team_obj = self.env['crm.team'].sudo()
        for user in self:
            user.x_mz_team_id = team_obj.search([('member_ids', '=', user.id)], limit=1)

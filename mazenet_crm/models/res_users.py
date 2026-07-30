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

    x_mz_role = fields.Selection(
        [
            ("agent", "Agent"),
            ("atl", "ATL"),
            ("tl", "TL"),
            ("manager", "BU Manager"),
            ("md", "MD"),
            ("admin", "CTO / Admin"),
        ],
        string="Mazenet Role",
        compute="_compute_x_mz_role",
        store=True,
        help="Highest Mazenet CRM role this user holds. TL and ATL share one security "
             "group (scope comes from the supervision map, not a separate group - see "
             "mz.supervision), so ATL is distinguished here purely by structure: a "
             "TL-group user whose own supervisor is ALSO TL-group is an ATL; one "
             "supervised by a Manager is a plain TL."
    )

    @api.depends('group_ids', 'x_mz_supervisor_id', 'x_mz_supervisor_id.group_ids')
    def _compute_x_mz_role(self):
        for user in self:
            if user.has_group("mazenet_crm.group_mz_admin"):
                user.x_mz_role = "admin"
            elif user.has_group("mazenet_crm.group_mz_md"):
                user.x_mz_role = "md"
            elif user.has_group("mazenet_crm.group_mz_manager"):
                user.x_mz_role = "manager"
            elif user.has_group("mazenet_crm.group_mz_tl"):
                supervisor = user.x_mz_supervisor_id
                if supervisor and supervisor.has_group("mazenet_crm.group_mz_tl") \
                        and not supervisor.has_group("mazenet_crm.group_mz_manager"):
                    user.x_mz_role = "atl"
                else:
                    user.x_mz_role = "tl"
            elif user.has_group("mazenet_crm.group_mz_agent"):
                user.x_mz_role = "agent"
            else:
                user.x_mz_role = False

    x_mz_supervisor_id = fields.Many2one(
        "res.users",
        string="Reports To",
        compute="_compute_mz_branch_user_ids",
        store=True,
        help="This user's direct supervisor per mz.supervision - lets the Pipeline be "
             "grouped by the real reporting chain (Team > Supervisor > Salesperson), "
             "nesting ATLs under their TL, TLs under their Manager, and so on, rather "
             "than a flat Salesperson list."
    )

    @api.depends()
    def _compute_mz_branch_user_ids(self):
        supervision_obj = self.env['mz.supervision'].sudo()
        for user in self:
            branch_ids = supervision_obj.get_branch(user)
            user.mz_branch_user_ids = [(6, 0, branch_ids)]
            user.x_mz_supervisor_id = supervision_obj.get_approver(user)

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

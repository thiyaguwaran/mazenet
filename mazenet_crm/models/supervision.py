# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError

class MzSupervision(models.Model):
    _name = "mz.supervision"
    _description = "Reporting Line"

    user_id = fields.Many2one("res.users", string="User", required=True, index=True, ondelete="cascade")
    supervisor_id = fields.Many2one("res.users", string="Supervisor", required=True, index=True, ondelete="cascade")
    role = fields.Selection([
        ("tl", "TL"),
        ("atl", "ATL"),
        ("manager", "Manager"),
    ], string="Supervisor Role", required=True)

    _uniq_user = models.Constraint(
        "UNIQUE(user_id)",
        "One direct supervisor per user."
    )

    @api.constrains('user_id', 'supervisor_id')
    def _check_recursion(self):
        for rec in self:
            if rec.user_id == rec.supervisor_id:
                raise ValidationError(_("A user cannot be their own supervisor."))
            current = rec.supervisor_id
            visited = {rec.user_id.id}
            while current:
                if current.id in visited:
                    raise ValidationError(_("Circular reference detected in supervision reporting lines."))
                visited.add(current.id)
                parent_sup = self.search([('user_id', '=', current.id)], limit=1)
                current = parent_sup.supervisor_id if parent_sup else False

    @api.model_create_multi
    def create(self, vals_list):
        recs = super(MzSupervision, self).create(vals_list)
        recs._recompute_all_user_branches()
        return recs

    def write(self, vals):
        res = super(MzSupervision, self).write(vals)
        self._recompute_all_user_branches()
        return res

    def unlink(self):
        res = super(MzSupervision, self).unlink()
        self.env['mz.supervision']._recompute_all_user_branches()
        return res

    @api.model
    def _recompute_all_user_branches(self):
        """Recomputes stored mz_branch_user_ids on res.users"""
        users = self.env['res.users'].search([('active', '=', True)])
        users._compute_mz_branch_user_ids()

    @api.model
    def get_branch(self, supervisor):
        """Returns set of user IDs transitively under supervisor"""
        supervisor_id = supervisor.id if isinstance(supervisor, models.BaseModel) else supervisor
        visited = set()
        to_process = [supervisor_id]
        branch_ids = set()

        while to_process:
            curr_id = to_process.pop(0)
            if curr_id in visited:
                continue
            visited.add(curr_id)
            subordinates = self.search([('supervisor_id', '=', curr_id)]).mapped('user_id.id')
            for sub_id in subordinates:
                if sub_id not in branch_ids:
                    branch_ids.add(sub_id)
                    to_process.append(sub_id)

        return list(branch_ids)

    @api.model
    def get_approver(self, user):
        """Returns direct supervisor user record for given user"""
        user_id = user.id if isinstance(user, models.BaseModel) else user
        sup_rec = self.search([('user_id', '=', user_id)], limit=1)
        return sup_rec.supervisor_id if sup_rec else False

    @api.model
    def get_release_chain(self, user):
        """Returns [direct_supervisor, next_up_supervisor, CTO/Admin] for 24h escalation"""
        user_id = user.id if isinstance(user, models.BaseModel) else user
        chain = []

        curr_user_id = user_id
        visited = set()
        while curr_user_id and curr_user_id not in visited:
            visited.add(curr_user_id)
            sup_rec = self.search([('user_id', '=', curr_user_id)], limit=1)
            if sup_rec and sup_rec.supervisor_id:
                chain.append(sup_rec.supervisor_id)
                curr_user_id = sup_rec.supervisor_id.id
            else:
                break

        # Fallback to CTO / Admin group user
        admin_group = self.env.ref('mazenet_crm.group_mz_admin', raise_if_not_found=False)
        cto_user = admin_group.user_ids[:1] if admin_group and admin_group.user_ids else self.env.ref('base.user_admin', raise_if_not_found=False)
        if cto_user and cto_user not in chain:
            chain.append(cto_user)

        return chain

    @api.model
    def get_escalated_approver(self, user):
        """Returns 1-level-up escalated supervisor for 24h escalation"""
        chain = self.get_release_chain(user)
        if len(chain) > 1:
            return chain[1]
        return False

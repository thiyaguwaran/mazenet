# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class MzMassAssignWizard(models.TransientModel):
    _name = "mz.mass.assign.wizard"
    _description = "Mass Assign Leads"

    lead_ids = fields.Many2many(
        "crm.lead", string="Leads", required=True,
        default=lambda self: self.env.context.get('active_ids', []),
    )
    x_assign_type = fields.Selection(
        [('team', 'Team'), ('internal', 'Internal')],
        string="Assign Type", required=True, default='team',
        help="Same meaning as the single-lead Assign Type on crm.lead: which pool the "
             "picked salesperson must belong to for EACH lead's own team - 'Team' "
             "(create_lead_id members) or 'Internal' (whoever's directly in a group "
             "ranked below yours in that team's configured hierarchy). DMT is exempt "
             "and always gets the full team roster, same as single-lead assignment."
    )
    user_id = fields.Many2one("res.users", string="Assign To", required=True)

    def action_apply(self):
        """Applies per-lead via the SAME crm.lead.write() every other edit goes
        through (not sudo, no bypass) - reuses _mz_check_assign_type_allowed's pool
        validation, RED-lock/team-transfer read-only gates, and the MD/Manager
        content-edit restrictions unchanged, one lead at a time, so a mixed-team
        selection can partially succeed instead of one bad lead blocking the batch.
        Note: because vals here always includes x_assign_type alongside user_id, a
        DMT user's narrow "user_id-only" waiver for a locked/transferred lead
        (content_touched == {"user_id"}, see crm_lead.py's write()) does not apply
        during a mass action - those leads correctly still report read-only rather
        than silently slipping through a single-record-shaped exemption."""
        self.ensure_one()
        if not self.lead_ids:
            raise UserError(_("No leads selected."))

        succeeded, failed = [], []
        for lead in self.lead_ids:
            try:
                with self.env.cr.savepoint():
                    lead.write({'x_assign_type': self.x_assign_type, 'user_id': self.user_id.id})
                succeeded.append(lead.name)
            except (AccessError, UserError, ValidationError) as e:
                failed.append(_("%(lead)s: %(error)s") % {'lead': lead.name, 'error': str(e)})

        if not succeeded:
            raise UserError(_(
                "None of the selected leads could be reassigned:\n%s"
            ) % '\n'.join(failed))

        message_parts = [_("Reassigned %(n)s lead(s) to %(user)s.") % {
            'n': len(succeeded), 'user': self.user_id.name,
        }]
        if failed:
            message_parts.append(_("%(n)s lead(s) could not be reassigned:\n%(details)s") % {
                'n': len(failed), 'details': '\n'.join(failed),
            })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Mass Assign"),
                'message': '\n\n'.join(message_parts),
                'type': 'warning' if failed else 'success',
                'sticky': bool(failed),
            },
        }

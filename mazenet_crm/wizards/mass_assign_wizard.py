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
    @api.model
    def _selection_x_assign_type(self):
        """Client spec (2026-09-09): DMT gets ONLY 'Team' here (they always hand a lead
        to a team, never pick a specific person themselves - the single-lead DMT+Team
        behavior already leaves user_id unset for the same reason). CTO/Admin/MD get
        BOTH (global cross-team reach). Every other team's ATL/TL/Manager only ever
        bulk-reassigns WITHIN their own team, so they get 'Internal' only. Unlike
        crm.lead's own x_assign_type, this wizard is a TransientModel opened fresh per
        session, so there's no per-record variability problem here; checking
        self.env.user directly is enough, no mirror-field workaround needed."""
        user = self.env.user
        is_dmt = self.env['crm.lead']._mz_user_is_dmt(user)
        is_cto_or_md = (
            user.has_group('mazenet_access_rights.group_mzr_cto_admin')
            or user.has_group('mazenet_access_rights.group_mzr_md')
        )
        if is_dmt:
            return [('team', 'Team')]
        if is_cto_or_md:
            return [('team', 'Team'), ('internal', 'Internal')]
        return [('internal', 'Internal')]

    @api.model
    def _default_x_assign_type(self):
        return self._selection_x_assign_type()[0][0]

    x_assign_type = fields.Selection(
        selection='_selection_x_assign_type',
        string="Assign Type", required=True, default=_default_x_assign_type,
        help="'Team' bulk-transfers the selected leads to a different Sales Team "
             "(client instruction, 2026-09-09) - user_id is cleared on each, same as "
             "the single-lead DMT+Team behavior, so the receiving team's lead assigns "
             "the actual salesperson afterwards. Only offered to DMT and CTO/Admin/MD. "
             "'Internal' picks a specific salesperson, restricted to whoever's directly "
             "in a group ranked below yours in EACH lead's own team hierarchy (DMT "
             "exempt, gets the full team roster)."
    )
    team_id = fields.Many2one(
        "crm.team", string="Sales Team",
        help="Shown/required only for 'Team' - the team every selected lead is bulk-"
             "transferred to."
    )
    user_id = fields.Many2one(
        "res.users", string="Assign To",
        help="Shown/required only for 'Internal' - the salesperson every selected lead "
             "is bulk-assigned to."
    )

    @api.constrains('x_assign_type', 'team_id', 'user_id')
    def _check_target_set(self):
        for wizard in self:
            if wizard.x_assign_type == 'team' and not wizard.team_id:
                raise ValidationError(_("Pick a Sales Team to transfer the selected leads to."))
            if wizard.x_assign_type == 'internal' and not wizard.user_id:
                raise ValidationError(_("Pick a Salesperson to assign the selected leads to."))

    def action_apply(self):
        """Applies per-lead via the SAME crm.lead.write() every other edit goes
        through (not sudo, no bypass) - reuses _mz_check_assign_type_allowed's pool
        validation, RED-lock/team-transfer read-only gates, and the MD/Manager
        content-edit restrictions unchanged, one lead at a time, so a mixed-team
        selection can partially succeed instead of one bad lead blocking the batch.

        'Team' is a plain team_id transfer (not a per-lead x_assign_type='team' write -
        that flavor is about picking a create_lead_id-pool salesperson WITHIN a lead's
        existing team, which isn't what a bulk team change means here), clearing
        user_id the same way the single-lead DMT+Team onchange does. 'Internal' keeps
        going through x_assign_type so _mz_check_assign_type_allowed's per-team pool
        check still applies to the picked salesperson.

        Note: because vals here never touches ONLY user_id (team mode also writes
        team_id; internal mode also writes x_assign_type), a DMT user's narrow
        "user_id-only" waiver for a locked/transferred lead (content_touched ==
        {"user_id"}, see crm_lead.py's write()) does not apply during a mass action -
        those leads correctly still report read-only rather than silently slipping
        through a single-record-shaped exemption."""
        self.ensure_one()
        if not self.lead_ids:
            raise UserError(_("No leads selected."))

        if self.x_assign_type == 'team':
            vals = {'team_id': self.team_id.id, 'user_id': False}
            target_name = self.team_id.name
        else:
            vals = {'x_assign_type': self.x_assign_type, 'user_id': self.user_id.id}
            target_name = self.user_id.name

        succeeded, failed = [], []
        for lead in self.lead_ids:
            try:
                with self.env.cr.savepoint():
                    lead.write(vals)
                succeeded.append(lead.name)
            except (AccessError, UserError, ValidationError) as e:
                failed.append(_("%(lead)s: %(error)s") % {'lead': lead.name, 'error': str(e)})

        if not succeeded:
            raise UserError(_(
                "None of the selected leads could be reassigned:\n%s"
            ) % '\n'.join(failed))

        message_parts = [_("Reassigned %(n)s lead(s) to %(target)s.") % {
            'n': len(succeeded), 'target': target_name,
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

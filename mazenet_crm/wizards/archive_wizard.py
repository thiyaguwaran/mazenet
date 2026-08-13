# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError

class MzArchiveWizard(models.TransientModel):
    _name = "mz.archive.wizard"
    _description = "Lead Archive Wizard"

    lead_id = fields.Many2one("crm.lead", string="Lead", required=True, default=lambda self: self.env.context.get('active_id'))
    reason = fields.Text(string="Archival Reason", required=True)
    request_ref = fields.Char(string="BU-Head Written Request Ref", required=True, help="Reference to the approved BU-Head request")

    def action_archive(self):
        self.ensure_one()
        u = self.env.user

        lead = self.lead_id
        if not lead:
            raise UserError(_("No lead specified for archival."))

        # Log chatter entry
        msg_body = _(
            "<b>Lead Archived by %s</b><br/>"
            "<b>BU-Head Request Ref:</b> %s<br/>"
            "<b>Reason:</b> %s"
        ) % (u.name, self.request_ref, self.reason)

        lead.message_post(body=msg_body)
        lead.with_context(mz_archive_wizard=True).write({'active': False})

        return {'type': 'ir.actions.act_window_close'}

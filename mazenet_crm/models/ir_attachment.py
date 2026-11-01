# -*- coding: utf-8 -*-
from odoo import _, models
from odoo.exceptions import AccessError


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    def unlink(self):
        """Attachments on the CRM custom fields are permanent (client requirement, 2026-11-01),
        deletable only by CTO / Admin. Blocks deleting any attachment that is linked from one of the lead's custom attachment
        fields, whichever route is used (widget, raw RPC, list view). Chatter attachments and
        files not linked to those fields are untouched. Superuser code (lead deletion, module
        uninstall, migrations) is not blocked, same as crm.lead.unlink()."""
        if self.ids and not self.env.su and not self.env.user.has_group(
                'mazenet_access_rights.group_mzr_cto_admin'):
            Lead = self.env['crm.lead']
            for fname in Lead._mz_attachment_field_names():
                field = Lead._fields[fname]
                self.env.cr.execute(
                    'SELECT 1 FROM "%s" WHERE "%s" IN %%s LIMIT 1' % (field.relation, field.column2),
                    [tuple(self.ids)],
                )
                if self.env.cr.fetchone():
                    raise AccessError(_("Only CTO / Admin can delete attachments."))
        return super().unlink()

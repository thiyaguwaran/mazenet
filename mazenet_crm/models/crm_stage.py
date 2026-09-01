# -*- coding: utf-8 -*-
from odoo import models, fields


class CrmStage(models.Model):
    _inherit = "crm.stage"

    # Stock crm.stage has no active field at all - M2-01 asks to "ARCHIVE (do not
    # delete) every stage that is not in these five pipeline sheets", which isn't
    # possible without this. Once present, Odoo's ORM automatically excludes
    # active=False records from every default search - including stage_id's own
    # Many2one domain on the lead form - so no other code needs to change.
    active = fields.Boolean(default=True)

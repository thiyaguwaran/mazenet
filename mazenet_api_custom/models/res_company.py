# -*- coding: utf-8 -*-
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    allow_api_config_edit = fields.Boolean(
        string="Allow API Configuration Edit", groups='base.group_no_one',
        help="When enabled, the API Configuration and the Server Details can be edited. "
             "Otherwise they are read only.")
